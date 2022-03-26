#!/usr/bin/env python3
import argparse
import asyncio
import json
import os
import re
import sys
from typing import List

from aiohttp import ClientConnectorError
from loguru import logger
from prometheus_client import Gauge, start_http_server
from pythclient.exceptions import SolanaException
from pythclient.pythclient import PythClient
from pythclient.ratelimit import RateLimit

from pyth_observer import get_key, get_solana_urls
from pyth_observer.coingecko import get_coingecko_prices, symbol_to_id_mapping
from pyth_observer.prices import Price, PriceValidator

logger.enable("pythclient")
RateLimit.configure_default_ratelimit(overall_cps=5, method_cps=3, connection_cps=3)

def get_publishers(network):
    """
    Get the mapping of publisher key --> names.
    """
    try:
        with open("publishers.json") as _fh:
            json_data = json.load(_fh)
    except (OSError, ValueError, TypeError, FileNotFoundError):
        logger.error("problem loading publishers.json only keys will be printed")
        json_data = {}
    return json_data.get(network, {})


def filter_errors(regexes, errors):
    filtered_errors = []
    for e in errors:
        skip = False
        for r in regexes:
            if re.match(r, f"{e.symbol}/{e.error_code}", re.IGNORECASE):
                skip = True
        if not skip:
            filtered_errors.append(e)
    return filtered_errors


async def main(args):
    program_key = get_key(network=args.network, type="program", version="v2")
    mapping_key = get_key(network=args.network, type="mapping", version="v2")
    http_url, ws_url = get_solana_urls(network=args.network)

    publishers = get_publishers(args.network)
    coingecko_prices = {}
    prom_prices = Gauge(
        "crypto_price", "Price", labelnames=["symbol", "publisher", "status"]
    )
    prom_price_account_errors = Gauge(
        "pyth_publisher_price_errors", "Price errors for publishers", labelnames=["symbol", "error_code"]
    )
    prom_publisher_price_errors = Gauge(
        "pyth_price_account_errors", "Price errors for price accounts", labelnames=["symbol", "publisher", "error_code"]
    )

    async def run_alerts():
        async with PythClient(
            solana_endpoint=http_url,
            solana_ws_endpoint=ws_url,
            first_mapping_account_key=mapping_key,
            program_key=program_key if args.use_program_accounts else None,
        ) as c:

            validators = {}

            logger.info("Starting pyth-observer against {}: {}", args.network, http_url)
            while True:
                try:
                    await c.refresh_all_prices()
                except (ClientConnectorError, SolanaException) as exc:
                    logger.error("{} refreshing prices: {}", exc.__class__.__name__, exc)
                    asyncio.sleep(0.4)
                    continue

                logger.trace("Updating product listing")
                try:
                    products = await c.get_products()
                except (ClientConnectorError, SolanaException) as exc:
                    logger.error("{} refreshing prices: {}", exc.__class__.__name__, exc)
                    asyncio.sleep(0.4)
                    continue

                for product in products:
                    all_errors = []
                    all_publishers = []
                    errors_by_publishers = {}

                    symbol = product.symbol
                    coingecko_price = coingecko_prices.get(product.attrs['base'])

                    # prevent adding duplicate symbols
                    if symbol not in validators:
                        # TODO: If publisher_key is not None, then only do validation for that publisher
                        validators[symbol] = PriceValidator(
                            key=args.publisher_key,
                            network=args.network,
                            symbol=symbol,
                            coingecko_price=coingecko_price
                        )
                    prices = await product.get_prices()

                    # Even though we iterate over a list of price accounts, each
                    # product currently has a single price account.
                    for _, price_account in prices.items():
                        price = Price(
                            slot=price_account.slot,
                            aggregate=price_account.aggregate_price_info,
                            product_attrs=product.attrs,
                            publishers=publishers,
                        )
                        price_account_errors = validators[symbol].verify_price_account(
                            price_account=price_account,
                            coingecko_price=coingecko_price,
                            include_noisy=args.include_noisy_alerts,
                        )
                        price_errors = validators[symbol].verify_price(
                            price=price,
                            include_noisy=args.include_noisy_alerts,
                        )

                        all_errors.extend(price_account_errors)
                        all_errors.extend(price_errors)

                        for price_comp in price_account.price_components:
                            # The PythPublisherKey
                            publisher_key = price_comp.publisher_key.key
                            price.quoters[publisher_key] = price_comp.latest_price_info
                            price.quoter_aggregates[publisher_key] = price_comp.last_aggregate_price_info

                            all_publishers.append(publisher_key)

                            if args.enable_prometheus:
                                prom_prices.labels(
                                    symbol=symbol,
                                    publisher=publisher_key,
                                    status=price.quoters[publisher_key].price_status.name,
                                ).set(price.quoters[publisher_key].price)

                    filtered_errors = filter_errors(args.ignore, all_errors) if args.ignore else all_errors

                    for error in filtered_errors:
                        if error.publisher_key in errors_by_publishers:
                            errors_by_publishers[error.publisher_key].append(error)
                        else:
                            errors_by_publishers[error.publisher_key] = [error]

                    if args.enable_prometheus:
                        # Report price account errors
                        if None in errors_by_publishers:
                            for error in errors_by_publishers[None]:
                                prom_price_account_errors.labels(
                                    symbol=symbol,
                                    error_code=error.error_code,
                                ).set(1)
                        else:
                            continue
                            prom_price_account_errors.labels(
                                symbol=symbol,
                                error_code="",
                            ).set(1)

                        # Report publisher price errors
                        for publisher in all_publishers:
                            if publisher in errors_by_publishers:
                                for error in errors_by_publishers[publisher]:
                                    prom_publisher_price_errors.labels(
                                        symbol=symbol,
                                        publisher=publisher,
                                        error_code=error.error_code,
                                    ).set(1)
                            else:
                                continue
                                prom_publisher_price_errors.labels(
                                    symbol=symbol,
                                    publisher=publisher,
                                    error_code="",
                                ).set(1)

                    # Send all notifications for a given symbol pair
                    await validators[symbol].notify(
                        filtered_errors,
                        slack_webhook_url=args.slack_webhook_url,
                        notification_mins=args.notification_snooze_mins,
                    )
                await asyncio.sleep(0.4)

    async def run_coingecko_get_price():
        nonlocal coingecko_prices
        while True:
            coingecko_prices = get_coingecko_prices([x for x in symbol_to_id_mapping])
            await asyncio.sleep(2)

    await asyncio.gather(run_alerts(), run_coingecko_get_price())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "-l",
        "--log-level",
        action="store",
        type=str.upper,
        choices=["INFO", "WARN", "ERROR", "DEBUG", "TRACE"],
        default="ERROR",
    )
    parser.add_argument(
        "-n",
        "--network",
        action="store",
        choices=["devnet", "mainnet", "testnet"],
        default="devnet",
    )
    parser.add_argument(
        "-k",
        "--publisher-key",
        help="The public key for a single publisher to monitor for",
    )
    parser.add_argument(
        "-u",
        "--use-program-accounts",
        action="store_true",
        default=False,
        help="Use getProgramAccounts to get all pyth data",
    )
    parser.add_argument(
        "--slack-webhook-url",
        default=os.environ.get("PYTH_OBSERVER_SLACK_WEBHOOK_URL"),
        help="Slack incoming webhook url for notifications. This is required to send alerts to slack",
    )
    parser.add_argument(
        "--notification-snooze-mins",
        type=int,
        default=0,
        help="Minutes between sending notifications for similar erroneous events",
    )
    parser.add_argument(
        "-N",
        "--include-noisy-alerts",
        action="store_true",
        default=False,
        help="Include alerts which might be excessively noisy when used for all publishers",
    )
    parser.add_argument(
        "-p",
        "--enable-prometheus",
        action="store_true",
        default=False,
        help="Enable Prometheus Monitoring exporter",
    )
    parser.add_argument(
        "--prometheus-port",
        type=int,
        default=9001,
        help="Prometheus Exporter port",
    )
    parser.add_argument(
        "--ignore",
        nargs="+",
        help="List of symbols and / or events to ignore. For e.g. 'Crypto.ORCA/USD' to ignore all ORCA alerts and 'FX.*/price-feed-offline' to ignore all price-feed-offline alerts for all FX pairs",
    )
    args = parser.parse_args()

    logger.remove()
    logger.add(sys.stderr, level=args.log_level)
    try:
        if args.enable_prometheus:
            logger.info(f"Starting Prometheus Exporter on port {args.prometheus_port}")
            start_http_server(port=args.prometheus_port)
        asyncio.run(main(args=args))
    except KeyboardInterrupt:
        logger.info("Exiting on CTRL-c")
