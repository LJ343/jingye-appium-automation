"""Offline exported-source checks. No phone, sockets or production entrypoints."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]


def main():
    if len(sys.argv)==3 and sys.argv[1]=='--suite':
        capture=io.StringIO()
        with contextlib.redirect_stdout(capture),contextlib.redirect_stderr(capture):
            suite=unittest.defaultTestLoader.discover(str(ROOT/sys.argv[2]))
            result=unittest.TextTestRunner(stream=capture).run(suite)
        if not result.wasSuccessful():
            sys.stderr.write(capture.getvalue());return 2
        print(json.dumps(dict(tests=result.testsRun,passed=True)));return 0
    def run(args):
        p=subprocess.run([str(a) for a in args],cwd=ROOT,capture_output=True,text=True,encoding='utf-8',timeout=120)
        if p.returncode:raise RuntimeError(p.stdout+p.stderr)
        return p.stdout
    counts={}
    for name,path in [('general','tests'),('reconciliation','workspace/skills/jingye-reconciliation/tests'),('platform','workspace/skills/jingye-platform-sync/tests')]:
        counts[name]=json.loads(run([sys.executable,'-B','-X','utf8',__file__,'--suite',path]))['tests']
    assert counts=={'general':96,'reconciliation':141,'platform':13}
    java_home=Path(os.environ.get('JAVA_HOME',r'C:\Program Files\Java\jdk-25.0.3'))
    javac=java_home/'bin/javac.exe';java=java_home/'bin/java.exe'
    source=ROOT/'android-companion/src/com/lj343/jingye/connectionguard'
    tests=ROOT/'android-companion/tests'
    package='com.lj343.jingye.connectionguard.'
    with tempfile.TemporaryDirectory(prefix='jingye-04-offline-') as temporary:
        out=Path(temporary)
        def compile_run(name,files,cls,marker):
            classes=out/name;classes.mkdir()
            run([javac,'--release','17','-encoding','UTF-8','-d',classes,*files])
            reply=run([java,'-cp',classes,cls]);assert marker in reply
            return reply
        # Explicit current test classes: the retained ExternalWaitReviewTest describes
        # historical 0.3 policy and is deliberately not compiled against 0.4.
        compile_run('policy',[source/'RecoveryController.java',source/'FailureBoundary.java',
                    tests/'com/lj343/jingye/connectionguard/RecoveryControllerTest.java'],
                    package+'RecoveryControllerTest','ALL 41 TESTS PASSED')
        compile_run('storage',[*sorted((ROOT/'android-companion/test-stubs').rglob('*.java')),
                    source/'RecoveryController.java',source/'StateStore.java',
                    tests/'com/lj343/jingye/connectionguard/StateStoreRetryTest.java'],
                    package+'StateStoreRetryTest','ALL 9 STORAGE TESTS PASSED')
        reply=compile_run('protocol',[source/'AddressProtocol.java',tests/'AddressProtocolTest.java'],
                          'AddressProtocolTest','PASS 12 protocol assertions')
        sys.path.insert(0,str(ROOT/'workspace/skills/jingye-reconciliation/scripts'))
        import jingye_wireless_address as report
        assert report.decode(reply.strip().splitlines()[-1].encode(),bytes.fromhex('02'*32))[-1]=='192.168.50.100'
    print(json.dumps(dict(passed=True,python=counts,python_total=sum(counts.values()),
                         helper_state=41,helper_storage=9,java_protocol_assertions=12,
                         shared_protocol_vector=True,device_actions=0,network_actions=0)))
    return 0


if __name__=='__main__':raise SystemExit(main())
