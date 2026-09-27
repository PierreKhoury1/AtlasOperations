import sys, shutil, os
from remotezip import RemoteZip
U="https://zenodo.org/records/15535461/files/Public_release_videos.zip?download=1"
S="Public_release_videos/test/YH2003/2023_06_02_09_20_42/videos/"
out=os.path.dirname(os.path.abspath(__file__))
with RemoteZip(U) as z:
    names=[i for i in z.infolist() if i.filename.startswith(S) and i.filename.endswith(".mp4") and "hololens" not in i.filename]
    print([ (i.filename[len(S):], round(i.file_size/1e6)) for i in names], flush=True)
    for i in names:
        dst=os.path.join(out, i.filename[len(S):])
        if os.path.exists(dst) and os.path.getsize(dst)==i.file_size: continue
        with z.open(i) as src, open(dst,"wb") as f: shutil.copyfileobj(src,f,1<<22)
        print("got",dst,flush=True)
print("DONE",flush=True)
