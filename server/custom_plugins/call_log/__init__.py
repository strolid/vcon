import asyncio
import redis.asyncio as redis
from lib.logging_utils import init_logger
import datetime
import whisper
from settings import REDIS_URL
import traceback
from server.lib.vcon_redis import VconRedis
import copy
from lib.sentry import init_sentry
from lib.phone_number_utils import get_e164_number

init_sentry()

r = redis.from_url(REDIS_URL, encoding="utf-8", decode_responses=True)
vcon_redis = VconRedis(redis_client=r)

logger = init_logger(__name__)


model = whisper.load_model("base")

default_options = {
    "name": "call_log",
    "ingress-topics": [],
    "transcribe": True,
    "min_transcription_length": 10,
    "deepgram": False,
    "egress-topics": [],
}


async def run(vcon_uuid):
    try:
        # Construct empty vCon, set meta data
        vCon = await vcon_redis.get_vcon(vcon_uuid)
        projection_index = -1
        for index, attachment in enumerate(vCon.attachments):
            if attachment.get("projection") == "call_log":
                projection_index = index
                break

        projection = get_projection(vCon)
        if projection_index > -1:
            vCon.attachments[projection_index] = projection
        else:
            vCon.attachments.append(projection)
        await vcon_redis.store_vcon(vCon)
        return vCon

    except Exception:
        logger.error("call_log plugin: error: \n%s", traceback.format_exc())
        logger.error("Shoot!")


def get_projection(vCon):
    projection = {}
    # Extract the parties from the vCon
    projection["projection"] = "call_log"
    for party in vCon.parties:
        if party["role"] == "customer":
            projection["customer_number"] = party["tel"]
            break

    main_agent, projection["disposition"] = get_main_agent_and_disposition(vCon)
    projection["extension"] = main_agent["extension"]
    projection["agent_name"] = main_agent["name"]

    projection["direction"] = vCon.attachments[0]["payload"]["direction"].upper()

    projection["created_on"] = datetime.datetime.now().strftime("%m/%d/%Y, %H:%M:%S")
    projection["modified_on"] = datetime.datetime.now().strftime("%m/%d/%Y, %H:%M:%S")
    projection["call_started_on"] = vCon.attachments[0]["payload"]["startedAt"]
    projection["id"] = vCon.uuid
    projection["lead_id"] = get_lead_id(vCon.attachments)
    projection["dialog"] = compute_dialog_projection(vCon.dialog)
    add_agent_extension_to_dialog(vCon, projection["dialog"])

    projection["duration"] = calculate_duration(vCon.dialog)
    dealer = vCon.attachments[0]["dealer"]
    if dealer:
        projection["dealer_cached_details"] = dealer
        projection["dealer_cxm_id"] = dealer["id"]
        if vCon.attachments[0]["payload"]["direction"].upper() == "OUT":
            projection["dealer_number"] = get_e164_number(dealer["outboundPhoneNumber"])
    else:  # this is needed to support the legacy queue name where dealer id is not part of it
        dealer_name = vCon.attachments[0]["payload"].get("dealerName")
        if dealer_name:
            projection["dealer_cached_details"] = {"name": dealer_name}
    # vCon.attachments.append(projection)
    return projection


def get_lead_id(attachments):
    for attachment in attachments:
        if attachment.get("lead"):
            return attachment["lead"]["id"]


def calculate_duration(dialog):
    duration = 0
    for dialog_item in dialog:
        duration += dialog_item["duration"]
    return duration


def get_agent_from_dialog_item(dialog_item, vCon):
    for party_idx in dialog_item["parties"]:
        party = vCon.parties[party_idx]
        if party["role"] == "agent":
            return party
    return None


# main agent/extenst is whoever last answered the call or the last agent if no one answered
def get_main_agent_and_disposition(vCon):
    main_dialog_item = None
    copy_dialog = compute_dialog_projection(vCon.dialog)
    dialog_reversed = list(reversed(copy_dialog))
    answered_dispositions = ["ANSWERED", "INTERNAL TRANSFER"]
    for dialog_item in dialog_reversed:
        if dialog_item["disposition"] in answered_dispositions:
            main_dialog_item = dialog_item
            break

    if main_dialog_item:
        main_disposition = main_dialog_item["disposition"]
        if main_disposition == "INTERNAL TRANSFER":
            main_disposition = "LOST INTERNAL TRANSFER"
    else:
        main_dialog_item = dialog_reversed[0]
        if main_dialog_item["direction"] == "out":
            main_disposition = "NO ANSWER"
        else:
            if len(copy_dialog) == 1 and main_dialog_item["duration"] < 4:
                main_disposition = "HUNG UP"
            else:
                main_disposition = "LOST"

    agent = get_agent_from_dialog_item(main_dialog_item, vCon)

    return agent, main_disposition


def add_agent_extension_to_dialog(vCon, dialog):
    for dialog_item in dialog:
        # get the agent's extension
        agent_idx = dialog_item["parties"][-1]
        dialog_item["agent_extension"] = vCon.parties[agent_idx]["extension"]
        dialog_item["agent_name"] = vCon.parties[agent_idx]["name"]


def dialog_disposition(dialog_item, is_last_dialog=True):
    disp = dialog_item["disposition"]
    if disp == "ANSWERED" and not is_last_dialog:
        return "INTERNAL TRANSFER"
    if disp != "MISSED":
        return disp
    if dialog_item["direction"].upper() == "OUT":
        return "NO ANSWER"
    if dialog_item["duration"] < 4 and is_last_dialog:
        return "HUNG UP"
    if dialog_item["duration"] < 12 and not is_last_dialog:
        return "DECLINED"
    return disp


def compute_dialog_projection(dialog):
    copied_dialog = copy.deepcopy(dialog)
    copied_dialog.sort(key=lambda x: x["start"])
    for ind, dialog_item in enumerate(copied_dialog):
        is_last_dialog = ind == (len(copied_dialog) - 1)
        dialog_item["disposition"] = dialog_disposition(dialog_item, is_last_dialog)
    return copied_dialog


async def start(opts=None):
    if opts is None:
        opts = copy.deepcopy(default_options)
    logger.info("Starting the call_log plugin")
    while True:
        try:
            p = r.pubsub(ignore_subscribe_messages=True)
            await p.subscribe(*opts["ingress-topics"])
            async for message in p.listen():
                vConUuid = message["data"]
                logger.info(f"call_log plugin: received vCon: {vConUuid}")
                await run(vConUuid)
                for topic in opts["egress-topics"]:
                    await r.publish(topic, vConUuid)
        except asyncio.CancelledError:
            logger.debug("call log plugin Cancelled")
            break
        except Exception:
            logger.error("call log plugin: error: \n%s", traceback.format_exc())
            logger.error("Shoot!")

    logger.info("call_log stopped")
