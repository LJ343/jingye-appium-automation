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
            result=unittest.TextTestRunner(stream=capture).run(unittest.defaultTestLoader.discover(str(ROOT/sys.argv[2])))
        if not result.wasSuccessful():
            sys.stderr.write(capture.getvalue());return 2
        print(json.dumps(dict(tests=result.testsRun,passed=True)));return 0
    def run(args):
        p=subprocess.run([str(a) for a in args],cwd=ROOT,capture_output=True,text=True,encoding='utf-8',timeout=150)
        if p.returncode:raise RuntimeError(p.stdout+p.stderr)
        return p.stdout
    counts={}
    for name,path in [('general','tests'),('reconciliation','workspace/skills/jingye-reconciliation/tests'),('platform','workspace/skills/jingye-platform-sync/tests')]:
        counts[name]=json.loads(run([sys.executable,'-B','-X','utf8',__file__,'--suite',path]))['tests']
    assert counts=={'general':100,'reconciliation':154,'platform':13}
    java_home=Path(os.environ.get('JAVA_HOME',r'C:\Program Files\Java\jdk-25.0.3'))
    javac=java_home/'bin/javac.exe';java=java_home/'bin/java.exe'
    companion=ROOT/'android-companion';source=companion/'src/com/lj343/jingye/connectionguard'
    tests=companion/'tests';package='com.lj343.jingye.connectionguard.'
    with tempfile.TemporaryDirectory(prefix='jingye-053-offline-') as temporary:
        out=Path(temporary)
        def compile_run(name,files,cls,marker):
            classes=out/name;classes.mkdir()
            run([javac,'--release','17','-encoding','UTF-8','-d',classes,*files])
            reply=run([java,'-cp',classes,cls]);assert marker in reply
            return reply
        test_package=tests/'com/lj343/jingye/connectionguard'
        compile_run('policy',[source/'RecoveryController.java',source/'FailureBoundary.java',test_package/'RecoveryControllerTest.java'],package+'RecoveryControllerTest','ALL 41 TESTS PASSED')
        storage_stubs=sorted((companion/'test-stubs').rglob('*.java'))
        compile_run('storage',[*storage_stubs,source/'RecoveryController.java',source/'StateStore.java',test_package/'StateStoreRetryTest.java'],package+'StateStoreRetryTest','ALL 9 STORAGE TESTS PASSED')
        trust=[source/n for n in ['TrustIdentity.java','TrustMessage.java','TrustLedger.java','TrustGuard.java']]
        compile_run('trust',[*trust,test_package/'TrustGuardTest.java'],package+'TrustGuardTest','ALL 56 TRUST TESTS PASSED')
        compile_run('window',[source/'TrustWindowSession.java',test_package/'TrustWindowSessionTest.java'],package+'TrustWindowSessionTest','ALL 9 WINDOW TESTS PASSED')
        compile_run('trust-storage',[*storage_stubs,*sorted((companion/'trust-test-stubs').rglob('*.java')),*trust,source/'TrustSettings.java',test_package/'TrustSettingsTest.java'],package+'TrustSettingsTest','ALL 14 TRUST STORAGE TESTS PASSED')
        reply=compile_run('protocol',[source/'AddressProtocol.java',tests/'AddressProtocolTest.java'],'AddressProtocolTest','PASS 12 protocol assertions')
        sys.path.insert(0,str(ROOT/'workspace/skills/jingye-reconciliation/scripts'))
        import jingye_wireless_address as report
        assert report.decode(reply.strip().splitlines()[-1].encode(),bytes.fromhex('02'*32))[-1]=='192.168.50.100'
    print(json.dumps(dict(passed=True,python=counts,python_total=sum(counts.values()),helper_state=41,
        helper_storage=9,trust_policy=56,trust_storage=14,window_session=9,java_protocol_assertions=12,
        shared_protocol_vector=True,device_actions=0,network_actions=0)))
    return 0

if __name__=='__main__':raise SystemExit(main())
