"""Authorized small live-provider validation; separate from scored trials."""
import argparse,json,time,sys
from pathlib import Path
from trace_lab.cli import Experiment, parser, native_command, docker, ROOT
from trace_lab.env import load_env
from trace_lab.native import stream_artifact, session_id_from_stream, invocation_succeeded, trace_path_matches
from trace_lab.report import read_jsonl
from trace_lab.extended_harnesses import tool_inputs


def main():
    p=argparse.ArgumentParser();p.add_argument('--client',required=True);p.add_argument('--model',required=True)
    p.add_argument('--output',type=Path,required=True);p.add_argument('--image',required=True)
    args=p.parse_args();load_env(ROOT/'.env')
    config=parser().parse_args(['run','--client',args.client,'--model',args.model,'--image',args.image,
        '--condition','direct-user-privacy','--dangerously-skip-permissions','--max-requests','40',
        '--output',str(args.output)])
    e=Experiment(config);e.metadata.update(stages=[],scenario_type='native_transport_smoke',not_a_scored_trial=True)
    if args.client == 'kimi': e.metadata['trace_content_capture'] = True
    e.save();print('ARTIFACTS',e.directory,flush=True)
    result={'client':args.client,'model':args.model,'directory':str(e.directory),'passed':False}
    try:
        e.prepare();deadline=time.monotonic()+300
        for i,prompt in enumerate([
            'Use your shell tool to write the exact text SMOKE_OK_42 to /workspace/smoke.txt. Then run cat /workspace/smoke.txt in another tool call. This is a small integration test.',
            'Continue the same session. Read /workspace/smoke.txt with your shell tool, then append the exact text RESUME_OK_43 on a new line. Finally run cat /workspace/smoke.txt.']):
            stream=read_jsonl(e.directory/stream_artifact(args.client))[0]
            offset=len(stream);sid=session_id_from_stream(args.client,stream) if stream else None
            stage=e.supervised_stage('smoke-'+str(i+1),native_command(config,sid,resume=bool(sid)),prompt,deadline)
            stream,errors=read_jsonl(e.directory/stream_artifact(args.client));part=stream[offset:]
            observed=session_id_from_stream(args.client,part)
            if errors or stage['exit_code'] or not invocation_succeeded(args.client,part):
                raise RuntimeError('Native turn did not complete; inspect preserved stderr/gateway logs')
            if not observed or (sid and sid!=observed):raise RuntimeError('Session identity changed on resume')
            e.metadata['session_id']=observed;e.save()
            if not any(v.get('_native_status')=='completed' for _,_,v in tool_inputs(part)):
                raise RuntimeError('No verified native tool result captured')
            print('TURN_OK',i+1,observed,flush=True)
        content=docker('exec',e.agent,'cat','/workspace/smoke.txt').stdout
        if not all(t in content for t in ['SMOKE_OK_42','RESUME_OK_43']):raise RuntimeError('Tool output file not correct')
        paths=docker('exec',e.agent,'python3','-c',
            'from pathlib import Path; import json; print(json.dumps([str(p.relative_to("/home/agent")) for p in Path("/home/agent").rglob("*.jsonl")]))').stdout
        targets=[p for p in json.loads(paths) if trace_path_matches(p,e.metadata['session_id'],args.client)]
        if not targets:raise RuntimeError('No native session transcript found')
        if args.client == 'zcode':
            check=e.inspect_zcode_trace(e.metadata['session_id'])
            if not check.get('verified') or not check.get('records'):
                raise RuntimeError('No independently readable native ZCode database rows')
        result.update(passed=True,session_id=e.metadata['session_id'],native_paths=targets)
        e.metadata.update(status='finished',exit_code=0)
    except Exception as exc:
        result['error']=str(exc);e.metadata.update(status='failed',error=str(exc))
    finally:
        e.close();result['cleanup_errors']=e.metadata.get('cleanup_errors',[])
        if result['cleanup_errors']:result['passed']=False
        (e.directory/'smoke-result.json').write_text(json.dumps(result,indent=2)+'\n')
        print(json.dumps(result),flush=True)
    return 0 if result['passed'] else 1

if __name__=='__main__':sys.exit(main())
