"""Automatic bounded hypotheses; no filename, fixed wrapper offset or page count.

Accepted fast paths precede this layer. Every candidate must satisfy explicit
structure/content constraints and independent validators; score alone cannot
turn an invalid candidate into a recovery.
"""
import hashlib
import json
import multiprocessing
from pathlib import Path
import re
import shutil
import tempfile

from .evidence import Ref, EvidenceError, collect, refs, summary_value, MAX_OBJECTS
from .document import RecoveredDocument

STRATEGY_VERSION = 'evidence-pdf-v1'
MAX_INPUT = 256*1024*1024
MAX_CANDIDATES = 8
WALL_SECONDS = 240
MEMORY_BYTES = 3*1024*1024*1024
MAX_OUTPUT = 512*1024*1024


def digest(data):return hashlib.sha256(data).hexdigest()


def disk_budget(folder):
    if sum(p.stat().st_size for p in folder.rglob('*') if p.is_file())>MAX_OUTPUT:
        raise EvidenceError('Recovery temporary disk budget')


def serialize(value):
    if isinstance(value,Ref):return f'{value.number} {value.generation} R'.encode()
    if isinstance(value,bytes):return value
    if isinstance(value,str):return value.encode('latin1')
    if value is True:return b'true'
    if value is False:return b'false'
    if value is None:return b'null'
    if isinstance(value,(int,float)):return str(value).encode()
    if isinstance(value,list):return b'['+b' '.join(serialize(v) for v in value)+b']'
    if isinstance(value,dict):return b'<< '+b' '.join(serialize(k)+b' '+serialize(v) for k,v in value.items())+b' >>'
    raise EvidenceError('Cannot serialize inferred PDF value')


def graph(mapping, root, repair=False):
    seen=set();pages=[];patches={}
    def visit(ref,parent=None,depth=0):
        if not isinstance(ref,Ref) or ref in seen or depth>128:raise EvidenceError('Page graph cycle/duplicate/depth')
        seen.add(ref);record=mapping.get(ref)
        if record is None or not isinstance(record.value,dict):raise EvidenceError('Missing page graph node')
        value=record.value;kind=value.get('/Type');changed=dict(value)
        if parent is not None and value.get('/Parent')!=parent:
            if not repair:raise EvidenceError('Page Parent contradiction')
            changed['/Parent']=parent
        if kind=='/Page':pages.append(ref);count=1
        elif kind=='/Pages':
            kids=value.get('/Kids')
            if not isinstance(kids,list) or not kids:raise EvidenceError('Missing/empty Kids; page order not proven')
            count=sum(visit(child,ref,depth+1) for child in kids)
            if value.get('/Count')!=count:
                if not repair:raise EvidenceError('Page Count contradiction')
                changed['/Count']=count
        else:raise EvidenceError('Non-page object in page graph')
        if changed!=value:patches[ref]=changed
        return count
    visit(root)
    if not pages:raise EvidenceError('No reachable pages')
    # Excludes silently lost Page objects; orphan order is not invented.
    all_pages={r for r,v in mapping.items() if isinstance(v.value,dict) and v.value.get('/Type')=='/Page'}
    if set(pages)!=all_pages:raise EvidenceError('Unreachable/orphan pages; complete order not proven')
    return pages,patches


def resolved(mapping,root,patches):
    seen=set();pending=[root]
    while pending:
        ref=pending.pop()
        if ref in seen:continue
        seen.add(ref)
        if ref not in mapping and ref not in patches:raise EvidenceError('Missing reachable object '+str(ref))
        value=patches.get(ref,mapping[ref].value if ref in mapping else None)
        pending.extend(refs(value))
        if len(seen)>MAX_OBJECTS:raise EvidenceError('Reference graph budget')
    return len(seen)


def rebuild(data,records,expanded,root,patches,target):
    """Retain original objects/streams and publish a new authoritative XRef stream."""
    mapping={r.ref:r for r in records+expanded}
    if len(mapping)!=len(records)+len(expanded):raise EvidenceError('Conflicting revisions require an intact revision chain')
    numbers={r.ref.number for r in records+expanded}|{r.number for r in patches}
    if len(numbers)!=len(set(mapping)|set(patches)):raise EvidenceError('Multiple live generations are ambiguous')
    new=max(numbers,default=0)+1
    if new>=MAX_OBJECTS:raise EvidenceError('Rebuilt xref size budget')
    positions={};compressed={};index={}
    for r in expanded:
        i=index.get(r.container,0);index[r.container]=i+1
        if r.ref not in patches:compressed[r.ref.number]=(r.container,i)
    with target.open('wb') as stream:
        out=BoundedOutput(stream,MAX_OUTPUT)
        out.write(b'%PDF-1.7\n%\xe2\xe3\xcf\xd3\n')
        for r in records:
            if r.ref in patches:continue
            positions[r.ref.number]=(out.tell(),r.ref.generation)
            out.write(data[r.start:r.end]+b'\n')
        for ref,value in sorted(patches.items(),key=lambda item:item[0].number):
            positions[ref.number]=(out.tell(),ref.generation)
            out.write(f'{ref.number} {ref.generation} obj\n'.encode()+serialize(value)+b'\nendobj\n')
        x=out.tell();positions[new]=(x,0);entries=bytearray()
        for number in range(new+1):
            kind,a,b=(1,*positions[number]) if number in positions else (2,*compressed[number]) if number in compressed else (0,0,65535 if number==0 else 0)
            entries+=bytes([kind])+a.to_bytes(8,'big')+b.to_bytes(4,'big')
        trailer={'/Type':'/XRef','/Size':new+1,'/Root':root,'/W':[1,8,4],'/Length':len(entries)}
        out.write(f'{new} 0 obj\n'.encode()+serialize(trailer)+b'\nstream\n'+entries+b'\nendstream\nendobj\n')
        out.write(f'startxref\n{x}\n%%EOF\n'.encode())


def stream_inventory(path):
    import pikepdf
    rows={}
    with pikepdf.open(path,attempt_recovery=False) as pdf:
        for obj in pdf.objects:
            if isinstance(obj,pikepdf.Stream):rows[obj.objgen]=digest(obj.read_raw_bytes())
    return rows


def verify(path,data,records,family,level,origin,transformation,assumptions):
    from ..validator import validate
    import pikepdf
    count,warning=validate(path,True)
    actual=stream_inventory(path);preserved=[];images=0
    for r in records:
        if r.stream_start is None:continue
        expected=digest(data[r.stream_start:r.stream_end]);key=(r.ref.number,r.ref.generation)
        if actual.get(key)!=expected:
            # Intact incremental PDFs may retain superseded stream generations.
            # They must remain at the identical byte extent, not be reinterpreted.
            with path.open('rb') as stream:
                stream.seek(r.stream_start);retained=stream.read(r.stream_end-r.stream_start)
            if digest(retained)!=expected:raise EvidenceError('Original stream not preserved: '+str(key))
        preserved.append({'object':list(key),'sha256':expected,
                          'source_range':[origin+r.stream_start,origin+r.stream_end],
                          'decoded_range':[r.stream_start,r.stream_end]})
        images+=r.value.get('/Subtype')=='/Image'
    with pikepdf.open(path,attempt_recovery=False) as pdf:
        metadata={'pdf_version':pdf.pdf_version,'catalog':list(pdf.Root.objgen),'pages_root':list(pdf.Root.Pages.objgen)}
        pages=[{'number':i+1,'object':list(p.obj.objgen),'mediabox':list(map(float,p.mediabox)),
                'rotate':int(p.obj.get('/Rotate',0))} for i,p in enumerate(pdf.pages)]
    import subprocess
    poppler=shutil.which('pdftoppm')
    if poppler:
        for number in sorted({1,count//2+1,count}):
            check=subprocess.run([poppler,'-f',str(number),'-l',str(number),'-scale-to','256','-singlefile',str(path)],
                                 stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,timeout=30)
            if check.returncode:raise EvidenceError('Independent Poppler render failed: '+check.stderr.decode(errors='replace')[:1000])
    model=RecoveredDocument(family,level,pages,resources={'original_streams':len(preserved),'preserved_streams':len(preserved),'image_streams':images},metadata=metadata,
        warnings=[warning] if warning else [],assumptions=assumptions,
        provenance={'strategy':STRATEGY_VERSION,'origin':origin,'transformations':transformation,
                    'preserved_streams':preserved},validation={'libqpdf':'PASS','MuPDF':'PASS','all_pages_rendered':count,
                    'Poppler_representative':'PASS' if poppler else 'UNAVAILABLE','qpdf_cli':'PASS' if not warning else 'UNAVAILABLE'}).to_dict()
    return model


class BoundedOutput:
    def __init__(self,stream,limit):
        self.stream,self.limit=stream,limit

    def write(self,data):
        if self.stream.tell()+len(data)>self.limit:raise EvidenceError('PDF output exceeds disk budget')
        return self.stream.write(data)

    def __getattr__(self,name):
        return getattr(self.stream,name)


def candidates(data,records,tables,report):
    variants=[];positions={row['origin'] for row in report['coordinate_hypotheses'] if isinstance(row['origin'],int) and row['origin']>=0}
    for pos in report['observations']['headers']:positions.add(pos)
    if len(positions)>MAX_CANDIDATES:raise EvidenceError('Wrapper candidate budget')
    positions={p for p in positions if data[p:p+5]==b'%PDF-'}
    for origin in sorted(positions):
        if origin>=len(data):continue
        following=[p for p in positions if p>origin]
        boundary=min(following,default=len(data))
        raw=data[origin:boundary]
        endings=list(re.finditer(rb'startxref\s+\d+\s+%%EOF',raw))
        if endings and following:raw=raw[:endings[-1].end()]+b'\n'
        variants.append((origin,raw,[]))
    return variants


def run(source,folder,depth=0):
    if source.stat().st_size>MAX_INPUT:raise EvidenceError('Source size budget')
    data=source.read_bytes()
    if len(data)>MAX_INPUT:raise EvidenceError('Source size budget')
    from ..strategies.containers import REGISTRY
    family=('PDF' if data.startswith(b'%PDF-') else 'JPEG' if data.startswith(b'\xff\xd8\xff') else
        next((kind.upper() for _,magic,kind in REGISTRY if data.startswith(magic)),'UNKNOWN_WRAPPER'))
    attempts=[];accepted=[]
    try:records,expanded,tables,report=collect(data)
    except EvidenceError as exc:
        records=[];expanded=[];tables=[];report={'coordinate_hypotheses':[],'observations':{'headers':[]},'scanner_errors':[{'reason':str(exc)}]}
    (folder/'source-evidence.json').write_text(json.dumps(report,indent=2))
    variants=candidates(data,records,tables,report)
    # First try exact variants. Each still needs graph, byte and renderer proof.
    for origin,raw,transformation in variants:
        disk_budget(folder)
        target=folder/f'candidate-{len(attempts)}.pdf'
        hypothesis={'kind':'embedded-original','origin':origin,'support':transformation,'assumptions':[]}
        try:
            if not re.search(rb'startxref\s+\d+\s+%%EOF\s*$',raw):
                raise EvidenceError('Missing terminal reference metadata; rebuild required')
            target.write_bytes(raw)
            import pikepdf
            with pikepdf.open(target,attempt_recovery=False) as pdf:
                if pdf.is_encrypted:raise EvidenceError('KEY_REQUIRED: encrypted PDF')
            rr,ee,tt,evidence=collect(raw)
            if evidence['scanner_errors']:raise EvidenceError('Scanner contradictions: '+str(evidence['scanner_errors'][:3]))
            # Original reference metadata can resolve valid incremental generations.
            model=verify(target,raw,rr,family,'EXACT',origin,transformation,[])
            hypothesis.update(valid=True,score=100,validation=model['validation'])
            accepted.append((target,model,hypothesis,evidence))
        except Exception as exc:
            hypothesis.update(valid=False,score=0,contradictions=[str(exc)[:2000]]);target.unlink(missing_ok=True)
        attempts.append(hypothesis)
    # Never choose between two complete embedded documents without source evidence.
    if len(accepted)>1:raise EvidenceError('Multiple validated embedded documents; selection ambiguous')
    if not accepted and depth<2:
        from ..strategies.containers import REGISTRY,decode
        for name,magic,kind in REGISTRY:
            if not data.startswith(magic):continue
            try:
                decoded=decode(kind,data)
                # Ordinary direct PDFs retain the existing audited container path.
                # This layer adds nested/encoded/structurally damaged members.
                if depth==0 and decoded.startswith(b'%PDF-'):
                    import io,pikepdf
                    try:
                        with pikepdf.open(io.BytesIO(decoded),attempt_recovery=False) as pdf:
                            from ..validator import check_tree
                            check_tree(pdf)
                            if not pdf.check_pdf_syntax():break
                    except Exception:pass
                nested=folder/'nested';nested.mkdir(exist_ok=True)
                member=nested/'member.pdf';member.write_bytes(decoded)
                recovered=run(member,nested,depth+1)
                if recovered.get('success'):
                    model=recovered['model']
                    model['source_family']=family
                    if model['recovery_level']=='EXACT':model['recovery_level']='DECODED'
                    model['provenance'].setdefault('container_layers',[]).insert(0,{'codec':name,'input_sha256':digest(data),'decoded_sha256':digest(decoded)})
                    h={'kind':'nested-container','valid':True,'score':95,'support':model['provenance']['container_layers']}
                    target=folder/'nested-recovered.pdf';shutil.copyfile(nested/recovered['file'],target)
                    attempts.append(h);accepted.append((target,model,h,report))
            except Exception as exc:attempts.append({'kind':name,'valid':False,'contradictions':[str(exc)]})
            break
    if not accepted:
        sources=[(0,data,records,expanded,tables,report,[])]
        for o,r,t in variants:
            try:sources.append((o,r,*collect(r),t))
            except EvidenceError as exc:attempts.append({'kind':'reference-rebuild','valid':False,'contradictions':[str(exc)]})
        for origin,raw,rr,ee,tt,evidence,transformation in sources:
            if len(attempts)>=MAX_CANDIDATES:break
            if evidence.get('scanner_errors') or not rr:continue
            if any(isinstance(r.value,dict) and r.value.get('/Filter')=='/Standard'
                   and all(k in r.value for k in ('/O','/U','/P')) for r in rr+ee):
                attempts.append({'kind':'reference-rebuild','valid':False,'contradictions':['KEY_REQUIRED: surviving PDF security handler']});continue
            if any(isinstance(t['trailer'],dict) and '/Encrypt' in t['trailer'] for t in tt):
                attempts.append({'kind':'reference-rebuild','valid':False,'contradictions':['KEY_REQUIRED: encryption metadata present']});continue
            mapping={r.ref:r for r in rr+ee}
            if len(mapping)!=len(rr)+len(ee):
                attempts.append({'kind':'reference-rebuild','valid':False,'contradictions':['Duplicate/revision objects need intact reference metadata']});continue
            catalogs=[r for r in rr+ee if isinstance(r.value,dict) and r.value.get('/Type')=='/Catalog']
            roots=[(r.ref,r.value.get('/Pages'),{}) for r in catalogs]
            if not roots:
                pages_roots=[r.ref for r in rr+ee if isinstance(r.value,dict) and r.value.get('/Type')=='/Pages' and '/Parent' not in r.value]
                for page_root in pages_roots:
                    ref=Ref(max(r.ref.number for r in rr+ee)+1)
                    roots.append((ref,page_root,{ref:{'/Type':'/Catalog','/Pages':page_root}}))
            for root,page_root,initial in roots:
                if len(attempts)>=MAX_CANDIDATES:break
                target=folder/f'candidate-{len(attempts)}.pdf';h={'kind':'reference-rebuild','origin':origin,'support':[], 'assumptions':[]}
                try:
                    disk_budget(folder)
                    pages,patches=graph(mapping,page_root,repair=True);patches.update(initial)
                    resolved_count=resolved(mapping,root,patches)
                    rebuild(raw,rr,ee,root,patches,target)
                    level='PAGE_LEVEL_RECOVERY' if initial else 'STRUCTURAL_REPAIR'
                    model=verify(target,raw,rr,family,level,origin,transformation,[])
                    model['provenance']['synthesized_structures']=['PDF header','cross-reference stream','trailer/startxref/EOF']+[
                        {'object':[ref.number,ref.generation],'value':summary_value(value),'reason':'unique surviving Kids graph'} for ref,value in patches.items()]
                    model['validation']['resolved_references']=resolved_count
                    h['support']=[{'resolved_references':resolved_count,'ordered_pages':len(pages),
                                   'preserved_streams':model['resources']['preserved_streams'],'catalog':[root.number,root.generation]}]
                    h.update(valid=True,score=90-len(patches),validation=model['validation'])
                    accepted.append((target,model,h,evidence))
                except Exception as exc:h.update(valid=False,score=0,contradictions=[str(exc)[:2000]]);target.unlink(missing_ok=True)
                attempts.append(h)
        if len(accepted)>1:
            hashes={digest(p.read_bytes()) for p,_,_,_ in accepted}
            if len(hashes)!=1:raise EvidenceError('Competing validated graph reconstructions are ambiguous')
            accepted=accepted[:1]
    if not accepted:
        from .images import recover as recover_images
        target=folder/'image-pages.pdf'
        try:
            disk_budget(folder)
            model=recover_images(data,target,family)
            if model:
                h={'kind':'ordered-original-images','valid':True,'score':70,'assumptions':model['assumptions']}
                attempts.append(h);accepted.append((target,model,h,report))
        except Exception as exc:
            attempts.append({'kind':'ordered-original-images','valid':False,'contradictions':[str(exc)]})
            target.unlink(missing_ok=True)
    result={'schema':1,'strategy':STRATEGY_VERSION,'attempts':attempts,'success':bool(accepted),
            'source_sha256':digest(data),'source_family':family}
    if accepted:
        target,model,hypothesis,evidence=accepted[0]
        model['provenance']['source_sha256']=digest(data)
        starts=report.get('observations',{}).get('startxref',[])
        model['provenance']['original_startxref']=starts[-1]['value'] if starts else None
        result.update(file=target.name,model=model,selected_hypothesis=hypothesis)
        (folder/'selected-evidence.json').write_text(json.dumps(evidence,indent=2))
    else:result['next_step']='No complete constrained PDF candidate; continue bounded container/image analysis. Unordered orphan images are not page-order evidence.'
    return result


def child(source,folder):
    from ..research_limits import constrain
    folder=Path(folder)
    try:
        constrain(MEMORY_BYTES,int(WALL_SECONDS));result=run(Path(source),folder)
    except Exception as exc:result={'success':False,'error':str(exc),'strategy':STRATEGY_VERSION}
    (folder/'result.json').write_text(json.dumps(result,indent=2))


def attempt(source,parent):
    folder=Path(tempfile.mkdtemp(prefix='.universal-recovery-',dir=parent))
    process=multiprocessing.get_context('spawn').Process(target=child,args=(str(source),str(folder)))
    process.start();process.join(WALL_SECONDS)
    if process.is_alive():process.terminate();process.join(5)
    if process.is_alive():process.kill();process.join()
    try:
        record=json.loads((folder/'result.json').read_text()) if (folder/'result.json').is_file() else {'success':False,'error':'Recovery process timeout/resource limit'}
        record['_scratch']=str(folder)
        if record.get('success'):record['_path']=str(folder/record['file'])
        return record
    except BaseException:shutil.rmtree(folder,ignore_errors=True);raise
