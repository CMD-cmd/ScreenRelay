#!/usr/bin/env python3
"""Bounded acceptance recorder: observe state files; never read UU AX or change screens."""
import argparse,datetime,json,pathlib,subprocess,time
parser=argparse.ArgumentParser();parser.add_argument('--seconds',type=float,default=300)
args=parser.parse_args();root=pathlib.Path(__file__).resolve().parent.parent
support=pathlib.Path.home()/'Library/Application Support/UUDisplayAutomation'
path=root/'evidence'/('session-events-'+datetime.datetime.now().strftime('%Y%m%d-%H%M%S')+'.jsonl')
previous=None;topology_key=None;end=time.monotonic()+min(args.seconds,600)
print(path,flush=True)
with path.open('x') as out:
 while time.monotonic()<end:
  try:
   status=json.loads((support/'status.json').read_text());signal=json.loads((support/'signal.json').read_text())
   row={'mode':status.get('mode'),'waiting_for':status.get('waiting_for'),'error':status.get('last_error'),
        'uu':signal.get('state'),'trusted':signal.get('trusted'),'reason':signal.get('reason'),
        'signal_timestamp':signal.get('timestamp'),'event_id':signal.get('event_id'),
        'display_revision':signal.get('display_revision'),'ax_reads':signal.get('ax_reads'),
        'notifications':signal.get('notifications'),'heartbeats':signal.get('heartbeats'),
        'fallback_reads':signal.get('fallback_reads'),'observer_supported':signal.get('observer_supported'),
        'metrics':status.get('metrics',{}),'recovery_pending':status.get('recovery_pending')}
   if row!=previous:
    key=(row['display_revision'],row['mode'])
    if key!=topology_key:
     row['topology']=json.loads(subprocess.check_output([str(support/'native-topology')],text=True,timeout=3));topology_key=key
    out.write(json.dumps({'observed_at':time.time(),**row},ensure_ascii=False)+'\n');out.flush();previous={k:v for k,v in row.items() if k!='topology'}
  except Exception as exc:
   out.write(json.dumps({'observed_at':time.time(),'recorder_error':str(exc)})+'\n');out.flush()
  time.sleep(.1)
print('Recorder finished',flush=True)
