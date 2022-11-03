import os
import logging
import logging.config
import boto3
import redis
import asyncio
import importlib
import sys
import shortuuid

from fastapi.applications import FastAPI
from fastapi_utils.tasks import repeat_every
from settings import AWS_KEY_ID, AWS_SECRET_KEY

chains = []

# Load FastAPI app
app = FastAPI.conserver_app

logger = logging.getLogger(__name__)
logging.config.fileConfig('./logging.conf')
logger.info('Conserver starting up')

# Setup redis
r = redis.Redis(host='localhost', port=6379, db=0)

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


async def run_execute_chain(conserver_chain):
    # This function takes a list of [blocks,links,chains, plugins, tasks, adapters] and executes them
    # in order. The first 
    chain_id = shortuuid.uuid()
    chain_tasks = []

    # The first link in the chain gets ingress vCons from 
    # a REDIS PUBSUB with ingress-vcons.  Then, we create a
    # new REDIS PUBSUB for the egress vCons called "block_egress".
    # This becomes the input for the next plugin in the chain, 
    # and so on.  A plugin can act as a filter by not sending
    # any vCons to the egress channel.  

    last_egress_key = None
    for plugin in conserver_chain:
        # Load the module with the same name: "adapter.quiq"
        # I suspect we will  need to extend this to actually use pythons package
        # loading mechanism, but this is a start.
        pluginObject = importlib.import_module(plugin)

        # This is where we create the options for the plugin,
        # which will be passed to the plugin's execute function.
        # We need to replace this with a more robust mechanism
        options = pluginObject.default_options

        # This is a unique name for the egress chanel for this plugin. We
        # can use this in the next plugin 
        block_egress = "{}_{}".format(plugin, chain_id)
        options['egress-topics'].append(block_egress)
        if last_egress_key:
            options['ingress-topics'] = [last_egress_key]
        last_egress_key = block_egress

        # Start the plugin, add it to the list of tasks
        # and add this task to the chain of tasks.
        task = asyncio.create_task(pluginObject.start(options), name=plugin)
        background_tasks.add(task)
        chain_tasks.append(task)

    logger.info("Chain tasks: {}".format(chain_tasks))



background_tasks = set()

@app.on_event("startup")
async def load_services():
    adapters = os.listdir("adapters")
    for adapter in adapters:
        try:
            new_adapter = importlib.import_module("adapters."+adapter)
            background_tasks.add(asyncio.create_task(new_adapter.start(), name=adapter))
            logger.info("Adapter started: %s", adapter)
        except Exception as e:
            logger.info("Error loading adapter: %s %s", adapter, e)

    plugins = os.listdir("plugins")
    available_blocks = []
    for plugin in plugins:
        available_blocks.append("plugins."+plugin)

    storages = os.listdir("storage")
    for storage in storages:
        available_blocks.append("storage."+storage)

    projections = os.listdir("data_projections")
    for projection in projections:
        available_blocks.append("data_projections."+projection)

    r.delete('available_blocks')
    r.sadd('available_blocks', *available_blocks)

    active_chains = r.smembers('active_chains')
    for chain in active_chains:
        chain = chain.decode("utf-8")
        chain = chain.split(",")
        logger.info("Chain: {}".format(chain))
        asyncio.create_task(run_execute_chain(chain))


@app.on_event("shutdown")
async def shutdown_background_tasks():
    logger.info("Shutting down background tasks")
    for task in background_tasks:
        task.cancel()
        await task
        logger.info("Task cancelled: %s", task)