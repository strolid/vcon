import asyncio
import datetime
import traceback

import redis.asyncio as redis
from lib.logging_utils import init_logger
from settings import REDIS_URL
from server.lib.vcon_redis import VconRedis
from .models import ShelbyLead, database
import copy


import sentry_sdk
from lib.sentry import init_sentry

init_sentry()


logger = init_logger(__name__)

default_options = {
    "name": "postgres",
    "ingress-topics": ["ingress-vcons"],
    "egress-topics": [],
}
options = {}

r = redis.from_url(REDIS_URL, encoding="utf-8", decode_responses=True)
vcon_redis = VconRedis(redis_client=r)
p = r.pubsub(ignore_subscribe_messages=True)

tod = datetime.datetime.now()
two_months = datetime.timedelta(days=60)
lead_not_before = tod - two_months


def normalize_phone(phone):
    phone_digits = "".join(d for d in phone if d.isdigit())
    if len(phone_digits) == 10:
        return f"+1 {phone_digits[:3]}-{phone_digits[3:6]}-{phone_digits[6:]}"
    elif len(phone_digits) == 11:
        return f"+{phone_digits[0]} {phone_digits[1:4]}-{phone_digits[4:7]}-{phone_digits[7:]}"

    return phone


async def run(vConUuid):
    try:
        # inbound_vcon = await r.json().get(f"vcon:{str(vConUuid)}", Path.root_path())
        inbound_vcon = await vcon_redis.get_vcon(vConUuid)

        # Check if the lead is already attached
        lead_attachment = None
        for attachment in inbound_vcon.attachments:
            if attachment.get("lead"):
                lead_attachment = attachment.get("lead")
                break

        if lead_attachment:  # If the lead is already attached, don't reattach it
            return

        customer_phone_number = None
        for party in inbound_vcon.parties:
            if party["role"] == "customer":
                phone_number = party["tel"]
                customer_phone_number = normalize_phone(phone_number)
                break

        # Fetch the leads from the CXM using the customer phone number
        lead_query = ShelbyLead.select().where(
            ShelbyLead.lookup_details["contact"]["Normalized Phone"]
            == customer_phone_number,
            ShelbyLead.created_on > lead_not_before,
        )

        dealer = inbound_vcon.attachments[0].get("dealer")
        if dealer:
            dealer_id = dealer.get("id")
            if dealer_id:
                lead_query = lead_query.where(ShelbyLead.dealer_id == dealer_id)

        lead = lead_query.order_by(ShelbyLead.created_on.desc()).first()

        if lead is None:
            return

        lead_info = {"lead": lead.__dict__["__data__"]}
        inbound_vcon.attachments.append(lead_info)
        await vcon_redis.store_vcon(inbound_vcon)

    except Exception:
        logger.error("stitcher plugin: error: \n%s", traceback.format_exc())


async def start(opts=None):
    if opts is None:
        opts = copy.deepcopy(default_options)
    logger.info("Starting the stitcher plugin!!!")
    while True:
        try:
            await p.subscribe(*opts["ingress-topics"])
            async for message in p.listen():
                vConUuid = message["data"]
                logger.info("stitcher plugin: received vCon: %s", vConUuid)
                database.connect(reuse_if_open=True)
                await run(vConUuid)
                for topic in opts["egress-topics"]:
                    await r.publish(topic, vConUuid)
                if not database.is_closed():
                    database.close()

        except asyncio.CancelledError:
            logger.debug("stitcher plugin Cancelled")
            break
        except Exception as e:
            logger.error("stitcher plugin: error: \n%s", traceback.format_exc())
            sentry_sdk.capture_exception(e)
        finally:
            if not database.is_closed():
                database.close()
    logger.info("stitcher plugin stopped")
