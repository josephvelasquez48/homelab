#!/usr/bin/env python3
import subprocess
import sys
import time
import uuid
name = 'homelab-restore-test-' + uuid.uuid4().hex[:10]
def run(args, **kw):
    return subprocess.check_output(args, timeout=300, **kw)
try:
    run(['docker','run','-d','--name',name,'--network','none','--memory','512m','--cpus','1','--tmpfs','/var/lib/postgresql/data:rw,size=256m','-e','POSTGRES_HOST_AUTH_METHOD=trust','-e','POSTGRES_USER=homelab','-e','POSTGRES_DB=homelab','pgvector/pgvector:pg17'])
    for attempt in range(60):
        if subprocess.run(['docker','exec',name,'pg_isready','-U','homelab'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode == 0:
            break
        time.sleep(1)
    else:
        raise RuntimeError('Isolated Postgres did not start')
    subprocess.run(['docker','exec','-i',name,'pg_restore','--exit-on-error','-U','homelab','-d','homelab'],stdin=sys.stdin.buffer,check=True,timeout=180)
    result = run(['docker','exec',name,'psql','-U','homelab','-d','homelab','-Atc',"SELECT 'documents=' || count(*) FROM documents; SELECT 'jobs=' || count(*) FROM jobs; SELECT 'vector_extension=' || extversion FROM pg_extension WHERE extname='vector';"])
    print(result.decode(),end='')
finally:
    subprocess.run(['docker','rm','-f',name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
