import asyncio
import redis.asyncio as redis
import asyncio
import logging
import vcon
from redis.commands.json.path import Path
import json
import os
import simplejson as json

from stable_whisper import load_model
from stable_whisper import stabilize_timestamps

from settings import LOG_LEVEL

r = redis.Redis(host='localhost', port=6379, db=0)

logger = logging.getLogger(__name__)
logger.setLevel(LOG_LEVEL)

default_options = {
    "name": "transcription",
    "ingress-topics": ["ingress-vcons"],
    "egress-topics":[],
}
model = load_model("base")

options = {}

async def run(vcon_uuid, opts=default_options, ):
    inbound_vcon = await r.json().get(f"vcon:{str(vcon_uuid)}", Path.root_path())

    vCon = vcon.Vcon()
    vCon.loads(json.dumps(inbound_vcon))
    original_analysis_count = len(vCon.analysis)

    annotated_vcon = vCon.transcribe(**options)

    new_analysis_count = len(annotated_vcon.analysis)

    logger.info("transcribe plugin: vCon: {} analysis was: {} now: {}".format(
        vcon_uuid, original_analysis_count, new_analysis_count))

    # If we added any analysis, save it
    if(new_analysis_count != original_analysis_count):
        vcon_json_string = annotated_vcon.dumps()
        json_vcon_object = json.loads(vcon_json_string) 

        try:
            await r.json().set("vcon:{}".format(vCon.uuid), Path.root_path(), json_vcon_object)
        except Exception as e:
            logger.error("transcription plugin: error: {}".format(e))



async def start(opts=default_options):
    logger.info("Starting the transcription plugin")
    try:
        p = r.pubsub(ignore_subscribe_messages=True)
        await p.subscribe(*opts['ingress-topics'])

        while True:
            try:
                message = await p.get_message()
                if message:
                    vConUuid = message['data'].decode('utf-8')
                    logger.info("transcribe plugin: received vCon: {}".format(vConUuid))
                    run(opts, vConUuid)
                    for topic in opts['egress-topics']:
                        await r.publish(topic, vConUuid)
                await asyncio.sleep(0.1)
            except Exception as e:
                logger.error("transcription plugin: error: {}".format(e))
    except asyncio.CancelledError:
        logger.debug("transcription Cancelled")

    logger.info("transcription stopped")    


