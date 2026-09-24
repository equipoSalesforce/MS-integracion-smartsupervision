"""Run SSV tests with provider sockets denied; optional dedicated local Redis only."""
import contextlib
import asyncio
import io
import logging
import os
from pathlib import Path
import socket
import threading
import sys
import unittest

root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root))
os.environ['TEST_REDIS_URL']='redis://127.0.0.1:56380/15'
os.environ['GOOGLE_SPREADSHEET_ID']='synthetic-offline-errors'
os.environ['GOOGLE_CATALOGS_SPREADSHEET_ID']='synthetic-offline-catalogs'
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
pair_context=threading.local()
original_pair=socket.socketpair
def local_pair(*args,**kwargs):
    pair_context.active=True
    try: return original_pair(*args,**kwargs)
    finally: pair_context.active=False
socket.socketpair=local_pair
original_connect=socket.socket.connect
original_connect_ex=socket.socket.connect_ex
def guarded_connect(sock,address):
    if isinstance(address,tuple) and address[0] in ('127.0.0.1','::1') and (address[1]==56380 or getattr(pair_context,'active',False)):
        return original_connect(sock,address)
    raise OSError('External network denied by post-QA test runner')
def guarded_connect_ex(sock,address):
    if isinstance(address,tuple) and address[0] in ('127.0.0.1','::1') and (address[1]==56380 or getattr(pair_context,'active',False)):
        return original_connect_ex(sock,address)
    return 10013
socket.socket.connect=guarded_connect
socket.socket.connect_ex=guarded_connect_ex
with contextlib.redirect_stdout(io.StringIO()):
    suite=unittest.defaultTestLoader.discover(str(root/'tests'),top_level_dir=str(root))
    logging.getLogger().handlers=[logging.NullHandler()]
    result=unittest.TextTestRunner(verbosity=1,stream=io.StringIO()).run(suite)
print('SSV_OFFLINE_TESTS',result.testsRun,'FAILURES',len(result.failures),'ERRORS',len(result.errors),'SKIPPED',len(result.skipped))
for case,reason in result.skipped:
    print('SKIP',case.id(),reason)
for case,error in result.failures+result.errors:
    print('FAIL',case.id(),error[-3500:])
if any(case.id().endswith('test_extender_lease_lua_script_integracion_redis') for case,_ in result.skipped):
    from unittest.mock import patch
    import redis.asyncio as redis
    from tests.test_queue_lock_watchdog import TestQueueLockWatchdog

    async def verify_watchdog():
        client=redis.from_url(os.environ['TEST_REDIS_URL'],decode_responses=True)
        try:
            with patch('app.db.redis.get_redis_client',return_value=client):
                await TestQueueLockWatchdog().test_extender_lease_lua_script_integracion_redis()
        finally:
            await client.aclose()
    asyncio.run(verify_watchdog())
    print('WATCHDOG_SKIPPED_TEST_SEPARATE_LOCAL_REDIS_PASS')
raise SystemExit(0 if result.wasSuccessful() else 1)
