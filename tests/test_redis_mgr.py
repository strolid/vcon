import os 
import sys
import uuid
import pytest
import random
import string
sys.path.append("server")
import server.redis_mgr as redis_mgr
from server.redis_mgr import set_key, get_key, delete_key

def get_random_string(length):
    # choose from all lowercase letter
    letters = string.ascii_lowercase
    result_str = ''.join(random.choice(letters) for i in range(length))
    return result_str

@pytest.mark.asyncio
async def test_get_and_read():
    redis_mgr.create_pool()
    print(sys.path)
    random_bytes = get_random_string(10)
    random_key = get_random_string(10)
    await set_key(random_key, random_bytes)
    read_back = await get_key(random_key)
    assert(read_back==random_bytes)
    await delete_key(random_key)
    result = await get_key(random_key)
    await redis_mgr.shutdown_pool()
    assert(result==None)

@pytest.mark.asyncio
async def test_unit_key():
    redis_mgr.create_pool()
    random_key = get_random_string(10)
    result = await get_key(random_key)
    await redis_mgr.shutdown_pool()
    assert(result==None)

@pytest.mark.asyncio
async def test_empty_key():
    try:
        redis_mgr.create_pool()
        result = await get_key("")
        await redis_mgr.shutdown_pool()
    except Exception as e:
        assert(type(e)==TypeError)
