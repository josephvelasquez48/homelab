#!/usr/bin/env python3
"""Run on the Pi. Uses temporary non-hostNetwork pods, never writes database data."""
import json
import os
import subprocess
import sys
import uuid
os.environ['KUBECONFIG']='/home/joe/.kube/config'
K=['kubectl']
def get(args):
    return json.loads(subprocess.check_output(K+args+['-o','json'],timeout=30))
image=get(['-n','backend','get','deploy','api'])['spec']['template']['spec']['containers'][0]['image']
baseline='--baseline' in sys.argv
probe='import socket,sys; s=socket.create_connection((sys.argv[1],int(sys.argv[2])),timeout=3); s.close()'
created=[]
try:
    cases=[]
    for node in ['joe','m1-node']:
        for namespace,labels,role in [('backend',{},'unapproved'),('monitoring',{'app':'api'},'wrong-namespace'),('backend',{'batch.kubernetes.io/job-name':'api-migrate'},'migration')]:
            name='db-policy-check-'+uuid.uuid4().hex[:10]
            pod={'apiVersion':'v1','kind':'Pod','metadata':{'name':name,'namespace':namespace,'labels':labels},'spec':{'nodeSelector':{'kubernetes.io/hostname':node},'automountServiceAccountToken':False,'restartPolicy':'Never','activeDeadlineSeconds':240,'containers':[{'name':'probe','image':image,'command':['python','-c','import time; time.sleep(240)'],'resources':{'requests':{'cpu':'10m','memory':'32Mi'},'limits':{'cpu':'100m','memory':'64Mi'}},'securityContext':{'runAsNonRoot':True,'runAsUser':65534,'allowPrivilegeEscalation':False,'capabilities':{'drop':['ALL']}}}]}}
            subprocess.run(K+['create','-f','-'],input=json.dumps(pod).encode(),check=True,stdout=subprocess.DEVNULL,timeout=30)
            created.append((namespace,name))
            cases.append((namespace,name,node,role))
    for ns,name,node,role in cases:
        subprocess.run(K+['-n',ns,'wait','--for=condition=Ready','pod/'+name,'--timeout=60s'],check=True,stdout=subprocess.DEVNULL,timeout=65)
        # DNS must work even when database traffic is denied.
        subprocess.run(K+['-n',ns,'exec',name,'--','python','-c',"import socket; socket.gethostbyname('postgres.data.svc.cluster.local')"],check=True,timeout=10)
        for host,port in [('postgres.data.svc.cluster.local',5432),('redis.backend.svc.cluster.local',6379)]:
            result=subprocess.run(K+['-n',ns,'exec',name,'--','python','-c',probe,host,str(port)],capture_output=True,timeout=12)
            allowed=result.returncode==0
            expected=baseline or (role=='migration' and port==5432)
            print(f'{node} {role} {port}: {"allowed" if allowed else "blocked"}',flush=True)
            if allowed!=expected:
                raise RuntimeError('Policy result differs from expectation')
    if not baseline:
        pods=get(['-n','backend','get','pods'])['items']
        for pod in pods:
            role=pod['metadata'].get('labels',{}).get('app')
            if role not in ['api','worker'] or pod['status']['phase']!='Running':
                continue
            name=pod['metadata']['name']
            for host,port in [('postgres.data.svc.cluster.local',5432),('redis.backend.svc.cluster.local',6379)]:
                subprocess.run(K+['-n','backend','exec',name,'--','python','-c',probe,host,str(port)],check=True,timeout=12)
            print(f'{role} on {pod["spec"]["nodeName"]}: both databases reachable',flush=True)
finally:
    for ns,name in created:
        subprocess.run(K+['-n',ns,'delete','pod',name,'--ignore-not-found','--wait=false'],stdout=subprocess.DEVNULL,timeout=15)
