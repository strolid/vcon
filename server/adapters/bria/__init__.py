import asyncio
import copy
import hashlib
import json
import traceback
from datetime import datetime, timedelta
from typing import Optional

import boto3
import jwt
import redis.asyncio as redis
import requests
import sentry_sdk
from lib.sentry import init_sentry
from lib.listen_list import listen_list
from lib.logging_utils import init_logger
from lib.phone_number_utils import get_e164_number
from settings import (
    AWS_KEY_ID,
    AWS_SECRET_KEY,
    ENV,
    GRAPHQL_JWT_SECRET_KEY,
    GRAPHQL_URL,
    REDIS_URL,
)

import vcon
from server import redis_mgr
from server.lib.vcon_redis import VconRedis

init_sentry()

logger = init_logger(__name__)
logger.info("Bria adapter loading")

default_options = {
    "name": "bria",
    "ingress-list": [f"bria-conserver-feed-{ENV}"],
    "egress-topics": ["ingress-vcons"],
}


def get_graphql_jwt_token():
    expired_after = datetime.utcnow() + timedelta(days=1)
    payload = {
        "sub": "conserver",
        "name": "conserver",
        "iss": "Strolid",
        "iat": datetime.utcnow(),
        "exp": expired_after,
    }
    return jwt.encode(payload, GRAPHQL_JWT_SECRET_KEY, algorithm="HS256")


def fetch_dealers_data_from_graphql():
    # Make sure we have token in the cookies
    jwt_token = get_graphql_jwt_token()

    query = """{
    dealers {
        id
        name
        outboundPhoneNumber
        team {
            id
            name
        }
    }
    }"""

    headers = {"Authorization": f"Bearer {jwt_token}"}
    response = requests.post(GRAPHQL_URL, headers=headers, json={"query": query})

    dealers_data = {
        str(dealer_data["id"]): dealer_data
        for dealer_data in response.json()["data"]["dealers"]
    }

    return dealers_data


async def get_dealer_data(dealer_id: str) -> Optional[dict]:
    redis_client = redis_mgr.get_client()
    dealers_data = await redis_client.json().get("dealers-data")
    if dealers_data is None:
        dealers_data = fetch_dealers_data_from_graphql()
        await redis_client.json().set("dealers-data", ".", dealers_data)
        await redis_client.expire("dealers-data", timedelta(minutes=30))
        logger.info("Received dealers_data from GraphQL: %s", len(dealers_data))
    else:
        logger.info("Found dealers_data in Redis: %s", len(dealers_data))

    return dealers_data.get(dealer_id)


def time_diff_in_seconds(start_time: str, end_time: str) -> int:
    """Returns time difference in seconds for given start and end time

    Args:
        start_time (str): start time in iso string format (Z)
        end_time (str): end time in iso string format (Z)

    Returns:
        int: number of seconds between start and end time
    """
    start_time = datetime.strptime(start_time, "%Y-%m-%dT%H:%M:%S.%fZ")
    end_time = datetime.strptime(end_time, "%Y-%m-%dT%H:%M:%S.%fZ")
    duration = end_time - start_time
    return duration.seconds


async def add_bria_attachment(vCon, body, opts):
    # Set the adapter meta so we know where the this came from

    cxm_dealer_id = body.get("dealerId")
    dealer = await get_dealer_data(cxm_dealer_id)
    adapter_meta = {
        "adapter": "bria",
        "adapter_version": "0.1.0",
        "src": opts["ingress-list"],
        "type": "call_completed",
        "received_at": datetime.now().isoformat(),
        "payload": body,
        "dealer": dealer,
    }
    vCon.attachments.append(adapter_meta)


def get_party_index(vcon, tel=None, extension=None):
    if tel:
        for ind, party in enumerate(vcon.parties):
            if party.get("tel") == tel:
                return ind
    if extension:
        for ind, party in enumerate(vcon.parties):
            if party.get("extension") == extension:
                return ind
    return -1


def add_dialog(vcon, body):
    start_time = body["startedAt"]
    end_time = body["endedAt"]
    duration = time_diff_in_seconds(start_time, end_time)
    state = body["state"]
    email = body.get("email")
    username = email.split("@")[0]
    if "." in username:
        first_name = username.split(".")[0].title()
        last_name = username.split(".")[1].title()
        full_name = first_name + " " + last_name
    else:
        full_name = username.title()
    dealer_did = get_e164_number(body.get("dialerId"))
    customer_number = get_e164_number(body.get("customerNumber"))
    extension = body.get("extension")
    direction = body.get("direction")

    customer_index = get_party_index(vcon, tel=customer_number)
    if customer_index == -1:
        customer_index = vcon.add_party(
            {
                "tel": customer_number,
                "role": "customer",
            }
        )
    agent_index = get_party_index(vcon, extension=extension)
    if agent_index == -1:
        agent_index = vcon.add_party(
            {
                "tel": dealer_did,
                "mailto": email,
                "name": full_name,
                "role": "agent",
                "extension": extension,
            }
        )
    start_time = start_time.replace("Z", "+00:00")
    vcon.add_dialog_external_recording(
        body=None,
        start_time=start_time,
        disposition=state,
        duration=duration,
        parties=[customer_index, agent_index],
        mime_type="audio/x-wav",
        file_name=f"{body['id']}.wav",
        external_url=None,
        direction=direction,
    )


async def handle_bria_call_started_event(body, r):
    logger.info("Processing call STARTED event %s", body)
    await persist_call_leg_detection_key(r, body)


async def dedup(r, body) -> Optional[vcon.Vcon]:
    """Check if this is a duplicate call id
       This can happen if the agent has multiple browser sessions open at the same time
       if it's an outbound call and it has a dealer number
       we update the vCon with that dealer number

    Args:
        body (dict): dict recived from sqs

    Returns:
        Optional[vcon.Vcon]: Return vCon if duplicate is detected
    """
    bria_call_id = body["id"]
    v_con = await get_vcon_by_bria_id(r, bria_call_id)
    if v_con:
        logger.info(
            "Duplicate bria id found. Probably due to multi browser open at same time."
        )
        direction = body.get("direction")
        dealer_number = body.get("dialerId")
        extension = body.get("extension")
        if direction == "out" and dealer_number:
            party_index = get_party_index(v_con, extension=extension)
            logger.info(
                f"party index : {party_index}, {dealer_number}, {body.get('dealerName')}"
            )
            v_con.parties[party_index]["tel"] = get_e164_number(dealer_number)
            v_con.attachments[0]["payload"]["dialerId"] = dealer_number
            v_con.attachments[0]["payload"]["dealerName"] = body.get("dealerName")
        return v_con


async def handle_bria_call_ended_event(body, opts, r):
    vcon_redis = VconRedis(redis_client=r)
    logger.info("Processing call ENDED event %s", body)
    vCon = await dedup(r, body)
    if not vCon:
        vCon = await get_same_leg_or_new_vcon(r, body, vcon_redis)
        add_dialog(vCon, body)
        await add_bria_attachment(vCon, body, opts)
    await vcon_redis.store_vcon(vCon)
    for egress_topic in opts["egress-topics"]:
        await r.publish(egress_topic, vCon.uuid)


def create_presigned_url(
    bucket_name: str, object_key: str, expiration: int = 3600
) -> str:
    """Generate a presigned URL to share an S3 object

    :param bucket_name: string
    :param object_key: string
    :param expiration: Time in seconds for the presigned URL to remain valid
    :return: Presigned URL as string. If error, returns None.
    """

    # Generate a presigned URL for the S3 object
    s3_client = boto3.client(
        "s3", aws_access_key_id=AWS_KEY_ID, aws_secret_access_key=AWS_SECRET_KEY
    )
    try:
        response = s3_client.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket_name, "Key": object_key},
            ExpiresIn=expiration,
        )
    except Exception as e:
        logger.error(e)  # TODO should we rethrow this error???
        return None

    # The response contains the presigned URL
    return response


def create_sha512_hash_for_s3_file(bucket_name: str, object_key: str) -> str:
    s3 = boto3.client(
        "s3", aws_access_key_id=AWS_KEY_ID, aws_secret_access_key=AWS_SECRET_KEY
    )
    # Retrieve the file from S3 and calculate the SHA-512 fingerprint
    response = s3.get_object(Bucket=bucket_name, Key=object_key)
    contents = response["Body"].read()
    fingerprint = hashlib.sha512(contents).hexdigest()
    return fingerprint


TEN_YEARS_SECONDS = 3.156e8


async def handle_bria_s3_recording_event(record, opts, redis_client):
    """Called when new s3 event is received.
    This function will update dialog object in vcon with correct url for
    recording and hash checksum

    Args:
        record (s3 event): s3 event object
        opts (dict): _description_
        redis_client (redis_client): Redis client
    """
    vcon_redis = VconRedis(redis_client=redis_client)
    logger.info("Processing s3 recording %s", record)
    s3_object_key = record["s3"]["object"]["key"]
    s3_bucket_name = record["s3"]["bucket"]["name"]
    bria_call_id = s3_object_key.replace(".wav", "")
    v_con = await get_vcon_by_bria_id(redis_client, bria_call_id)
    if not v_con:
        return
    for index, dialog in enumerate(v_con.dialog):
        if dialog.get("filename") == f"{bria_call_id}.wav":
            # TODO https:// find a way to get https link with permenent access
            v_con.dialog[index]["url"] = create_presigned_url(
                s3_bucket_name, s3_object_key, TEN_YEARS_SECONDS
            )
            v_con.dialog[index]["signature"] = create_sha512_hash_for_s3_file(
                s3_bucket_name, s3_object_key
            )
            v_con.dialog[index]["alg"] = "SHA-512"
    await vcon_redis.store_vcon(v_con)
    for egress_topic in opts["egress-topics"]:
        await redis_client.publish(egress_topic, v_con.uuid)


async def handle_list(list_name, r, opts):
    async for data in listen_list(r, list_name):
        try:
            logger.info(f"We got data from {list_name} list {data}")
            payload = json.loads(data)
            records = payload.get("Records")
            if records:
                for record in records:
                    await handle_bria_s3_recording_event(record, opts, r)
            else:
                body = json.loads(payload.get("Message"))
                event_type = payload["MessageAttributes"]["kind"]["Value"]
                if event_type == "call_ended":
                    await handle_bria_call_ended_event(body, opts, r)
                elif event_type == "call_started":
                    await handle_bria_call_started_event(body, r)
                else:
                    logger.info(f"Ignoring the Event Type : {event_type}")
        except Exception as e:
            logger.error(e)
            sentry_sdk.capture_exception(e)


async def get_vcon_by_bria_id(r, bria_id: str) -> Optional[vcon.Vcon]:
    """Retrives the vcon from redis for givin bria_id

    Args:
        bria_id (str): bria id

    Returns:
        Optional[vcon.Vcon]: Returns vocon for given bria id
    """
    # lookup the vCon in redis using bria ID
    # FT.SEARCH idx:adapterIdsIndex '@adapter:{bria} @id:{f8be045704cb4ea98d73f60a88590754}'
    result = await r.ft(index_name="idx:adapterIdsIndex").search(
        f"@adapter:{{bria}} @id:{{{bria_id}}}"
    )
    if len(result.docs) <= 0:
        return
    v_con = vcon.Vcon()
    v_con.loads(result.docs[0].json)
    return v_con


async def get_same_leg_or_new_vcon(r, body, vcon_redis) -> vcon.Vcon:
    """Try to detect if this is the same call and if so don't create a new vCon but add this
        as a call stage to the existing vCon

    Returns:
        vcon.Vcon: New or existing vcon
    """
    direction = body.get("direction")
    redis_key = call_leg_detection_key(body)
    logger.info("computed_redis_key is %s", redis_key)
    vcon_id = await r.get(redis_key)
    v_con = None
    if vcon_id:
        v_con = await vcon_redis.get_vcon(vcon_id)
        logger.info(f"Found the key {redis_key} - Updating the existing vcon")

    # https://strolid-inc.sentry.io/issues/3938726105/
    if not v_con:
        v_con = vcon.Vcon()
        v_con.set_uuid("strolid.com")
        if direction == "in":
            await r.set(redis_key, v_con.uuid)
        logger.info(f"Key NOT found {redis_key}- Created a new vcon")

    logger.info(f"The vcon id is {v_con.uuid}")
    await r.expire(redis_key, 60)
    return v_con


def call_leg_detection_key(body: dict) -> str:
    dealer_number = get_e164_number(body.get("dialerId"))
    customer_number = get_e164_number(body.get("customerNumber"))
    direction = body.get("direction")
    return f"bria:{dealer_number}:{customer_number}:{direction}"


async def persist_call_leg_detection_key(r, body):
    key = call_leg_detection_key(body)
    logger.info(f"Trying to persist the key - {key}")
    if await r.exists(key):
        await r.persist(key)
        logger.info(f"Persisted the key - {key}")


async def start(opts=None):
    """Starts the bria adaptor as co-routine. This adaptor will run till it's killed.

    Args:
        opts (Defalut options, optional): Options which controls behaviour of this adaptor. Defaults to None.
    """
    if opts is None:
        opts = copy.deepcopy(default_options)
    logger.info("Starting the bria adapter")
    # Setup redis
    r = redis.from_url(REDIS_URL, encoding="utf-8", decode_responses=True)

    try:
        logger.info("Bria started")
        tasks = []
        for ingress_list in opts["ingress-list"]:
            task = asyncio.create_task(handle_list(ingress_list, r, opts))
            tasks.append(task)
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        logger.info("Bria Cancelled")
    except Exception:
        logger.error("bria adaptor error:\n%s", traceback.format_exc())

    logger.info("Bria adapter stopped")
