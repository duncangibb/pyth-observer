#!/usr/bin/env python3
import argparse
import asyncio
import json
import os
import re
import sys
import importlib

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


def init_notifiers(names):
    """
    Given the array of --notifier= args, load the appropriate modules and
    initialise an object with any args.
    Return an array of initialised objects
    """

    notifiers = []
    for i in names:
        args = i.split(sep="=", maxsplit=1)
        if len(args) == 0:
            raise ValueError("Notifier name not provided")
        if len(args) == 1:
            # Ensure we always have a param container, even if it is a dummy
            args.append(None)

        try:
            module = importlib.import_module(args[0])
        except ModuleNotFoundError:
            raise NameError(f'Notifier Module "{args[0]}" could not be found')
        notifier = module.Notifier(args[1])
        notifiers.append(notifier)

    return notifiers


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
    coingecko_prices_last_updated_at = {}
    gprice = Gauge(
        "crypto_price", "Price", labelnames=["symbol", "publisher", "status"]
    )

    notifiers = init_notifiers(args.notifier)

    async def run_alerts():
        nonlocal coingecko_prices_last_updated_at
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
                    logger.error(
                        "{} refreshing prices: {}", exc.__class__.__name__, exc
                    )
                    asyncio.sleep(0.4)
                    continue

                logger.trace("Updating product listing")
                try:
                    products = await c.get_products()
                except (ClientConnectorError, SolanaException) as exc:
                    logger.error(
                        "{} refreshing prices: {}", exc.__class__.__name__, exc
                    )
                    asyncio.sleep(0.4)
                    continue

                for product in products:
                    errors = []
                    symbol = product.symbol
                    coingecko_price = coingecko_prices.get(product.attrs["base"])
                    coingecko_price_last_updated_at = (
                        coingecko_prices_last_updated_at.get(product.attrs["base"])
                    )
                    # prevent adding duplicate symbols
                    if symbol not in validators:
                        # TODO: If publisher_key is not None, then only do validation for that publisher
                        validators[symbol] = PriceValidator(
                            key=args.publisher_key,
                            network=args.network,
                            symbol=symbol,
                            coingecko_price=coingecko_price,
                            coingecko_price_last_updated_at=coingecko_price_last_updated_at,
                        )
                    prices = await product.get_prices()

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
                            coingecko_price_last_updated_at=coingecko_price_last_updated_at,
                            include_noisy=args.include_noisy_alerts,
                        )
                        if price_account_errors:
                            errors.extend(price_account_errors)

                        for price_comp in price_account.price_components:
                            # The PythPublisherKey
                            publisher = price_comp.publisher_key.key

                            price.quoters[publisher] = price_comp.latest_price_info
                            price.quoter_aggregates[
                                publisher
                            ] = price_comp.last_aggregate_price_info

                            if args.enable_prometheus:
                                gprice.labels(
                                    symbol=symbol,
                                    publisher=publisher,
                                    status=price.quoters[publisher].price_status.name,
                                ).set(price.quoters[publisher].price)

                        # Where the magic happens!
                        price_errors = validators[symbol].verify_price(
                            price=price,
                            include_noisy=args.include_noisy_alerts,
                        )
                        if price_errors:
                            errors.extend(price_errors)

                    filtered_errors = (
                        filter_errors(args.ignore, errors) if args.ignore else errors
                    )
                    # Send all notifications for a given symbol pair
                    await validators[symbol].notify(
                        filtered_errors,
                        notifiers,
                        notification_mins=args.notification_snooze_mins,
                    )
                    if product.attrs["asset_type"] == "Crypto":
                        # check if coingecko price exists
                        coingecko_prices_last_updated_at[product.attrs["base"]] = (
                            coingecko_price and coingecko_price["last_updated_at"]
                        )
                await asyncio.sleep(0.4)

    async def run_coingecko_get_price():
        nonlocal coingecko_prices
        while True:
            coingecko_prices = await get_coingecko_prices(
                [x for x in symbol_to_id_mapping]
            )

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
        choices=["devnet", "mainnet", "testnet", "pythtest", "pythnet"],
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
        help="List of symbols and / or events to ignore. "
        "For e.g. 'Crypto.ORCA/USD' to ignore all ORCA alerts and "
        "'FX.*/price-feed-offline' to ignore all price-feed-offline "
        "alerts for all FX pairs",
    )
    parser.add_argument(
        "--notifier",
        action="append",
        help="Specify a notification system to be used.  Parameters can be "
        "passed to the notifier by separating with an equals sign",
    )
    args = parser.parse_args()

    # we might have an old slack config option
    #
    if not args.notifier:
        if args.slack_webhook_url is not None:
            slack = args.slack_webhook_url
            args.notifier = [f"pyth_observer.notifiers.slack={slack}"]
        else:
            args.notifier = ["pyth_observer.notifiers.logger"]

    logger.remove()
    logger.add(sys.stderr, level=args.log_level)
    try:
        if args.enable_prometheus:
            logger.info(f"Starting Prometheus Exporter on port {args.prometheus_port}")
            start_http_server(port=args.prometheus_port)
        asyncio.run(main(args=args))
    except KeyboardInterrupt:
        logger.info("Exiting on CTRL-c")
