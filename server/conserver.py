import os
import sys
import logging
import logging.config
import json
import urllib.request
import datetime
import importlib

import asyncio
import boto3
import redis
import uvicorn

from fastapi import status
from fastapi.requests import Request
from fastapi.exceptions import HTTPException
from fastapi.applications import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi_utils.tasks import repeat_every
from starlette.responses import JSONResponse, HTMLResponse
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates
from pydub import AudioSegment
from deepgram import Deepgram
import motor.motor_asyncio
import json
import jose.utils
import jose.jws
from pydantic_models import VconModel, PyObjectId

from settings import AWS_KEY_ID, AWS_SECRET_KEY, AWS_BUCKET

# Our local modules``
sys.path.append("..")
import vcon

logger = logging.getLogger(__name__)
logging.config.fileConfig('./logging.conf')
logger.info('Conserver starting up')



# Setup redis
r = redis.Redis(host='localhost', port=6379, db=0)

# Load FastAPI app
app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

origins = ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Start third-party services
client = motor.motor_asyncio.AsyncIOMotorClient(os.environ["MONGODB_URL"])
db = client.conserver

# Environment variables
VCON_VERSION = "0.1.1"
MIMETYPE="audio/wav"



@app.on_event("startup")
@repeat_every(seconds=1)
def check_sqs():
    sqs = boto3.resource('sqs', region_name='us-east-1', aws_access_key_id=AWS_KEY_ID, aws_secret_access_key=AWS_SECRET_KEY)
    queue_names = r.smembers('queue_names')
    try:
        for queue_name in queue_names:
            q = queue_name.decode("utf-8") 
            queue = sqs.get_queue_by_name(QueueName=q)
            for message in queue.receive_messages():
                message.delete()
                r.rpush(q, message.body)
    except Exception as e:
        logger.info("Error: {}".format(e))


background_tasks = set()

@app.on_event("startup")
async def load_services():
    adapters = os.listdir("adapters")
    for adapter in adapters:
        try:
            new_adapter = importlib.import_module("adapters."+adapter)
            background_tasks.add(asyncio.create_task(new_adapter.start()))
            logger.info("Adapter started: %s", adapter)
        except Exception as e:
            logger.info("Error loading adapter: %s %s", adapter, e)

    plugins = os.listdir("plugins")
    for plugin in plugins:
        try:
            new_plugin = importlib.import_module("plugins."+plugin)
            background_tasks.add(asyncio.create_task(new_plugin.start()))
            logger.info("Plugin started: %s", plugin)
        except Exception as e:
            logger.info("Error loading plugin: %s %s", plugin, e)

    storages = os.listdir("storage")
    for storage in storages:
        try:
            new_storage = importlib.import_module("storage."+storage)
            background_tasks.add(asyncio.create_task(new_storage.start()))
            logger.info("Storage started: %s", storage)
        except Exception as e:
            logger.info("Error loading storage: %s %s", storage, e)

    projections = os.listdir("data_projections")
    for projection in projections:
        try:
            new_projection = importlib.import_module("data_projections."+projection)
            background_tasks.add(asyncio.create_task(new_projection.start()))
            logger.info("Projection started: %s", projection)
        except Exception as e:
            logger.info("Error loading projection: %s %s", projection, e)


@app.on_event("shutdown")
async def shutdown_background_tasks():
    logger.info("Shutting down background tasks")
    for task in background_tasks:
        task.cancel()
        await task
        logger.info("Task cancelled: %s", task)





# Routes for the web server

# Home page route
@app.get("/", response_class=HTMLResponse)
async def homepage(request: Request):
    vcons = await db["vcons"].find().sort("created_at",-1 ).to_list(100)
    return templates.TemplateResponse("index.html", {"request": request, "vCons": vcons})

# Conversation Route
@app.get("/conversations/{page}", response_class=HTMLResponse)
async def conversations(request: Request, page: int):
    # Paginate the vCons
    length = 12
    vcons = await db["vcons"].find().sort("created_at",-1 ).skip(page*length).limit(length).to_list(length)
    return templates.TemplateResponse("conversations.html", {"request": request, "vCons": vcons, "page": page, "length": length})


# This is the endpoint for the vcon creation that comes from
# the SNS topic
@app.post("/vcon", response_class=HTMLResponse)
async def post_vcon(request: Request):
    ingress_msg = await request.json()
    ingress_vcon = json.loads(ingress_msg["Message"])

    # Construct empty vCon, set meta data
    vCon = vcon.Vcon()
    # Add some basic call META data
    caller = ingress_vcon["payload"]["cdr"]["src"]
    called = ingress_vcon["payload"]["cdr"]["dst"]
    vCon.set_party_tel_url(caller)
    vCon.set_party_tel_url(called)

    recording_url = ingress_vcon["payload"]["recording"]["url"]
    # Remove query string
    host = recording_url.split("?")[0]
    # The file name is the last part of the URL
    recording_filename = host.split("/")[-1]

    try:
        # Download the recording
        logger.info("Downloading recording from: %s", recording_url)
        recording_bytes = urllib.request.urlopen(recording_url).read()
        vCon.add_dialog_inline_recording(
        recording_bytes,
        ingress_vcon["payload"]["cdr"]["starttime"],
        ingress_vcon["payload"]["cdr"]["duration"],
        [0, 1], # parties recorded
        "audio/x-wav", # MIME type
        recording_filename)
        logger.info("Recording downloaded")

    except urllib.error.HTTPError as err:
        error_msg = "Error retrieving recording from " + recording_url
        error_type = "HTTPError"
        error_time = datetime.datetime.now().strftime("%m/%d/%Y, %H:%M:%S")
        vCon.attachments.append({"error_msg": error_msg, "error_type": error_type, "error_time": error_time})

    # Save the original RingPlan JSON in the vCon
    vCon.attachments.append(ingress_vcon)


    # Save the vCon to the database
    json_string = vCon.dumps()
    vcon_dict = json.loads(json_string)
    insert_one_result = await db["vcons"].insert_one(vcon_dict)

    logger.info("Saving to S3")
    # Save the vCon to S3
    s3 = boto3.resource(
    's3',
    region_name='us-east-1',
    aws_access_key_id=AWS_KEY_ID,
    aws_secret_access_key=AWS_SECRET_KEY
    )
    s3.Bucket(AWS_BUCKET).put_object(Key=str(insert_one_result.inserted_id), Body=json_string)
    logger.info("Saved to S3")

    return JSONResponse(status_code=status.HTTP_200_OK)

@app.get(
    "/vcon/{id}", response_description="Computer Readable vCon", response_model=VconModel
)
async def show_vcon(request: Request, id: PyObjectId):
    vcon = await db["vcons"].find_one({"_id": id})
    return vcon  

@app.get("/details/{id}", response_class=HTMLResponse)
async def show_vcon(request: Request, id: str):
    logger.info("Hello!")
    vcon = await db["vcons"].find_one({"_id": id})
    if vcon is None:
        raise HTTPException(status_code=404, detail=f"Vcon {id} not found")

        
    for index, dialog in enumerate(vcon['dialog']): 
        wav_filename = "static/{}_{}.wav".format(id, index)
        mp3_filename = "static/{}_{}.mp3".format(id, index)
        ogg_filename = "static/{}_{}.ogg".format(id, index)
        unknown_filename = "static/{}_{}".format(id, index)
        decoded_body = jose.utils.base64url_decode(bytes(dialog["body"], 'utf-8'))
        f = open(unknown_filename, "wb")
        f.write(decoded_body)
        f.close()
        # convert mp3 file to wav file
        sound = AudioSegment.from_file(unknown_filename)
        sound.export(mp3_filename, format="mp3")
        sound.export(wav_filename, format="wav")
        sound.export(ogg_filename, format="ogg")

        # Check to see if we have a transcript, create one if not
        need_transcript = True
        for analysis in vcon['analysis']:
            if analysis['type'] == 'transcript':
                need_transcript = False
                break
        if need_transcript:
            # Transcribe it
            dg_client = Deepgram(settings.DEEPGRAM_KEY)
            audio = open(wav_filename, 'rb')
            source = {
            'buffer': audio,
            'mimetype': MIMETYPE
            }
            transcription = await dg_client.transcription.prerecorded(source, 
                {   'punctuate': True,
                    'multichannel': False,
                    'language': 'en',
                    'model': 'general',
                    'punctuate': True,
                    'tier':'enhanced',
                })

            analysis_element = {}
            analysis_element["type"] = "transcript"
            analysis_element["dialog"] = index
            analysis_element["body"] = transcription
            analysis_element["encoding"] = "json"
            analysis_element["vendor"] = "deepgram"
            vcon['analysis'] = []
            vcon['analysis'].append(analysis_element)
            await db["vcons"].update_one({"_id": id}, {"$push": { 'analysis': analysis_element}})

            # Update S3
            logger.info("Transcription complete")
        else:
            logger.info("Transcription already exists")

    return templates.TemplateResponse("vcon.html", {"request": request, "vcon": vcon})




@app.get("/vcon/{id}", response_class=JSONResponse)
async def get_vcon(id: PyObjectId, version: str = "latest"):
    if version == "latest":
        vcon = await db["vcons"].find_one({"_id": id})
    else:
        vcon = await db["vcons"].find_one({"_id": id, "version": version})
    return vcon

@app.put(
    "/vcon/{id}", response_description="Update a single vcon", response_model=VconModel
)
@app.put(
    "/vcon/{id}/{version}", response_description="Update a single vcon", response_model=VconModel
)

@app.delete(
    "/vcon/{id}", response_description="Delete a single vcon", response_model=VconModel
)
@app.delete(
    "/vcon/{id}/{version}", response_description="Delete a single vcon", response_model=VconModel
)

@app.delete("/vcon/{id}", response_description="Delete a vcon")
async def delete_vcon(id: str):
    delete_result = await db["vcons"].delete_one({"_id": id})

    if delete_result.deleted_count == 1:
        return JSONResponse(status_code=status.HTTP_204_NO_CONTENT)

    raise HTTPException(status_code=404, detail=f"Vcon {id} not found")



if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
