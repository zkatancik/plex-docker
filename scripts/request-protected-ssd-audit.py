#!/Users/zack/.homebrew/opt/python@3.14/bin/python3.14
"""User-authenticated, read-only SSD audit. Never part of scheduled monitoring."""
import os,stat,hashlib,json,subprocess,shlex,secrets,datetime
PYTHON='/Users/zack/.homebrew/opt/python@3.14/bin/python3.14'
READER='/Users/zack/plex-docker/scripts/protected-ssd-reader.py'
EXPECTED='7f2b7603f160e58996a0a373983875431c4d7725285fc8bda90127f61ffbb6f1'
FLAGS=os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW

def directory(base,path,device):
 fd=os.dup(base)
 try:
  for part in path.split('/'):
   child=os.open(part,FLAGS,dir_fd=fd)
   if os.fstat(child).st_dev!=device:
    os.close(child);raise RuntimeError('External filesystem refused')
   os.close(fd);fd=child
  return fd
 except BaseException:os.close(fd);raise

def main():
 base=os.open('/',FLAGS)
 for part in ('System','Volumes','Data'):
  child=os.open(part,FLAGS,dir_fd=base);os.close(base);base=child
 device=os.fstat(base).st_dev
 scripts=directory(base,'Users/zack/plex-docker/scripts',device)
 logs=directory(base,'Users/zack/Library/Logs/storage-maintenance',device)
 try:
  reader=os.open('protected-ssd-reader.py',os.O_RDONLY|os.O_NOFOLLOW,dir_fd=scripts)
  try:
   s=os.fstat(reader)
   assert stat.S_ISREG(s.st_mode) and s.st_dev==device and s.st_uid==os.getuid() and s.st_nlink==1 and s.st_size<8192
   data=os.read(reader,8192)
   assert hashlib.sha256(data).hexdigest()==EXPECTED,'Reader changed; review before authentication'
  finally:os.close(reader)
  result={'requested_at':datetime.datetime.now().astimezone().isoformat(),'scope':'Named internal SSD metadata only','reader_sha256':EXPECTED,'authentication_timeout_seconds':180,'reader_process_alarm_seconds':12}
  command=' '.join(shlex.quote(x) for x in ['/usr/bin/perl','-e','alarm 12; exec @ARGV','/usr/bin/nice','-n','19',PYTHON,'-I',READER])
  script='do shell script '+json.dumps(command)+' with administrator privileges'
  print('Waiting for the macOS administrator dialog. The read-only worker has a separate 12-second alarm.',flush=True)
  try:
   p=subprocess.run(['/usr/bin/osascript','-e',script],capture_output=True,text=True,timeout=180)
   result['returncode']=p.returncode
   if p.returncode==0:
    assert len(p.stdout)<16384
    report=json.loads(p.stdout)
    assert report.get('uid')==0 and report.get('scope')=='/System/Volumes/Data'
    result['status']='administrator_metadata_collected';result['report']=report
   else:
    result['status']='administrator_check_failed';result['error']=p.stderr[:1200]
  except subprocess.TimeoutExpired:
   result['status']='authentication_or_check_timed_out';result['error']='No automatic retry.'
  except (ValueError,AssertionError) as error:
   result['status']='invalid_reader_result';result['error']=str(error)[:1200]
  result['finished_at']=datetime.datetime.now().astimezone().isoformat()
  payload=(json.dumps(result,indent=2)+'\n').encode();assert len(payload)<24576
  name='protected-ssd-admin-result.json'
  try:
   s=os.stat(name,dir_fd=logs,follow_symlinks=False)
   assert stat.S_ISREG(s.st_mode) and s.st_dev==device and s.st_uid==os.getuid() and s.st_nlink==1
  except FileNotFoundError:pass
  temp='.'+name+'.'+secrets.token_hex(6)+'.tmp'
  fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=logs)
  try:
   written=0
   while written<len(payload):written+=os.write(fd,payload[written:])
  finally:os.close(fd)
  os.replace(temp,name,src_dir_fd=logs,dst_dir_fd=logs)
  print(json.dumps(result),flush=True)
 finally:os.close(logs);os.close(scripts);os.close(base)

if __name__=='__main__':main()
