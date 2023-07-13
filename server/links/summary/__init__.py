from server.lib.vcon_redis import VconRedis
from lib.logging_utils import init_logger
import openai

logger = init_logger(__name__)

default_options = {
    "prompt": "Summarize this conversation: ",
}


def get_transcription(vcon, index):
    for a in vcon.analysis:
        if a["dialog"] == index and a['type'] == 'transcript':
            return a
    return None


def get_summary(vcon, index):
    for a in vcon.analysis:
        if a["dialog"] == index and a['type'] == 'summary':
            return a
    return None


def summarize(transcript):
    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Summarize this transcript in a few sentences, then indicate if they customer was frustrated or not, and if agent was helpful?"},
        {"role": "assistant", "content": transcript}
    ]
    sentiment_result = openai.ChatCompletion.create(
        model="gpt-3.5-turbo-16k",
        messages=messages
    )
    return sentiment_result["choices"][0]["message"]["content"]


async def run(
    vcon_uuid,
    opts=default_options,
):
    merged_opts = default_options.copy()
    merged_opts.update(opts)
    opts = merged_opts
    logger.info("Starting summary for vCon: %s", vcon_uuid)
    openai.api_key = opts["OPENAI_API_KEY"]
    # Cannot create redis client in global context as it will wait on async event
    # loop which may go away.
    vcon_redis = VconRedis()
    vCon = await vcon_redis.get_vcon(vcon_uuid)

    for index, dialog in enumerate(vCon.dialog):
        transcription = get_transcription(vCon, index)
        if not transcription:
            logger.info("No transcript found for vCon: %s", vCon.uuid)
            continue

        transcription_text = transcription['body']['transcript']
        summary = get_summary(vCon , index)
        # See if it already has summary
        if summary:
            logger.info("Dialog %s already summarized in vCon: %s", index, vCon.uuid)
            continue
        summary = summarize(transcription_text)
        logger.info("Summarized vCon: %s", vCon.uuid)
        vCon.add_analysis_transcript(
            index, summary, "openai", analysis_type="summary"
        )
    await vcon_redis.store_vcon(vCon)

    # Return the vcon_uuid down the chain.
    # If you want the vCon processing to stop (if you are filtering them, for instance)
    # send None
    return vcon_uuid
