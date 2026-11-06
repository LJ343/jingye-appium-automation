"""Offline exported-source checks only; never contacts a phone or network."""
import contextlib,io,json,os,subprocess,sys,tempfile,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def main():
    if len(sys.argv)==3 and sys.argv[1]=='--suite':
        capture=io.StringIO()
        with contextlib.redirect_stdout(capture),contextlib.redirect_stderr(capture):
            suite=unittest.defaultTestLoader.discover(str(ROOT/sys.argv[2]))
            result=unittest.TextTestRunner(stream=capture).run(suite)
        if not result.wasSuccessful():
            sys.stderr.write(capture.getvalue());return 2
        print(json.dumps({'tests':result.testsRun,'passed':True}));return 0
    counts={}
    for name,path in [('general','tests'),('reconciliation','workspace/skills/jingye-reconciliation/tests'),('platform','workspace/skills/jingye-platform-sync/tests')]:
        p=subprocess.run([sys.executable,'-B','-X','utf8',__file__,'--suite',path],cwd=ROOT,capture_output=True,text=True,encoding='utf-8',timeout=120)
        if p.returncode:sys.stderr.write(p.stdout+p.stderr);return 2
        counts[name]=json.loads(p.stdout)['tests']
    if not(counts['general']>=81 and counts['reconciliation']==141 and counts['platform']==13):return 2
    java_home=Path(os.environ.get('JAVA_HOME',r'C:\Program Files\Java\jdk-25.0.3'))
    package=ROOT/'android-companion/src/com/lj343/jingye/connectionguard'
    test=ROOT/'android-companion/tests'
    with tempfile.TemporaryDirectory(prefix='jingye-export-java-') as output:
        files=[package/n for n in ['AddressProtocol.java','RecoveryController.java','FailureBoundary.java']]
        files += list(test.rglob('*.java'))
        p=subprocess.run([str(java_home/'bin/javac.exe'),'--release','17','-encoding','UTF-8','-d',output,*map(str,files)],capture_output=True,text=True,encoding='utf-8',timeout=60)
        if p.returncode:sys.stderr.write(p.stderr);return 2
        for cls,marker in [('com.lj343.jingye.connectionguard.RecoveryControllerTest','ALL 31 TESTS PASSED'),('com.lj343.jingye.connectionguard.ExternalWaitReviewTest','INDEPENDENT 9 TESTS PASSED'),('AddressProtocolTest','PASS 12 protocol assertions')]:
            p=subprocess.run([str(java_home/'bin/java.exe'),'-cp',output,cls],capture_output=True,text=True,encoding='utf-8',timeout=30)
            if p.returncode or marker not in p.stdout:sys.stderr.write(p.stdout+p.stderr);return 2
            if cls=='AddressProtocolTest':
                sys.path.insert(0,str(ROOT/'workspace/skills/jingye-reconciliation/scripts'))
                import jingye_wireless_address as report
                assert report.decode(p.stdout.strip().splitlines()[-1].encode(),bytes.fromhex('02'*32))[-1]=='192.168.50.100'
    print(json.dumps({'passed':True,'python':counts,'python_total':sum(counts.values()),'helper_state':31,'helper_external_wait':9,'java_protocol_assertions':12,'shared_protocol_vector':True,'device_actions':0,'network_actions':0}));return 0
if __name__=='__main__':raise SystemExit(main())
