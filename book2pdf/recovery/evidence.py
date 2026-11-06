"""Bounded PDF lexical evidence; stream payloads are never scanned as objects.

This is an evidence reader, not a replacement PDF renderer. Strict libqpdf and
MuPDF validation must independently accept any document built from its records.
"""
from dataclasses import dataclass, asdict
import hashlib
import re
import zlib

MAX_OBJECTS = 100_000
MAX_DECODED = 64 * 1024 * 1024
WS = b'\x00\t\r\n\x0c '
OBJECT = re.compile(rb'(?<![\w])([0-9]+)\s+([0-9]+)\s+obj\b')
NUMBER = re.compile(rb'[+-]?(?:\d+\.?\d*|\.\d+)')


class EvidenceError(ValueError):
    pass


@dataclass(frozen=True)
class Ref:
    number: int
    generation: int = 0


class Reader:
    def __init__(self, data, pos=0, end=None):
        self.data, self.pos, self.end = data, pos, len(data) if end is None else end
        self.tokens = 0

    def skip(self):
        while self.pos < self.end:
            if self.data[self.pos] in WS:
                self.pos += 1
            elif self.data[self.pos] == 37:
                while self.pos < self.end and self.data[self.pos] not in b'\r\n': self.pos += 1
            else: break

    def value(self, depth=0):
        self.skip(); self.tokens += 1
        if depth > 64 or self.tokens > 500_000 or self.pos >= self.end:
            raise EvidenceError('PDF value depth/token/extent limit')
        d=self.data; start=self.pos
        if d[start:start+2] == b'<<':
            self.pos += 2; result={}
            while True:
                self.skip()
                if d[self.pos:self.pos+2] == b'>>':self.pos += 2; return result
                key=self.value(depth+1)
                if not isinstance(key,str) or not key.startswith('/') or key in result:
                    raise EvidenceError('Invalid/duplicate PDF dictionary key')
                result[key]=self.value(depth+1)
        if d[start] == 91:
            self.pos += 1; result=[]
            while True:
                self.skip()
                if self.pos < self.end and d[self.pos] == 93:self.pos += 1; return result
                result.append(self.value(depth+1))
        if d[start] == 40:
            nesting=1; self.pos += 1
            while self.pos < self.end and nesting:
                ch=d[self.pos];self.pos += 1
                if ch==92:self.pos += 1
                elif ch==40:nesting += 1
                elif ch==41:nesting -= 1
                if self.pos-start > MAX_DECODED:raise EvidenceError('PDF string limit')
            if nesting:raise EvidenceError('Unclosed PDF string')
            return d[start:self.pos]
        if d[start] == 60:
            stop=d.find(b'>',start+1,self.end)
            if stop<0:raise EvidenceError('Unclosed PDF hex string')
            self.pos=stop+1;return d[start:self.pos]
        if d[start] == 47:
            self.pos += 1
            while self.pos<self.end and d[self.pos] not in WS+b'()<>[]{}/%':self.pos += 1
            raw=d[start:self.pos]
            raw=re.sub(rb'#([0-9a-fA-F]{2})',lambda m:bytes([int(m[1],16)]),raw)
            return raw.decode('latin1')
        match=NUMBER.match(d,start,self.end)
        if match:
            self.pos=match.end();raw=match.group();value=float(raw) if b'.' in raw else int(raw)
            save=self.pos;self.skip();other=NUMBER.match(d,self.pos,self.end)
            if isinstance(value,int) and other and other.group().isdigit():
                self.pos=other.end();self.skip()
                if d[self.pos:self.pos+1]==b'R' and (self.pos+1==self.end or d[self.pos+1] in WS+b'[]<>/%'):
                    self.pos+=1;return Ref(value,int(other.group()))
            self.pos=save;return value
        for literal,value in ((b'true',True),(b'false',False),(b'null',None)):
            if d[start:start+len(literal)]==literal:self.pos+=len(literal);return value
        raise EvidenceError('Unrecognized PDF token at '+str(start))


def refs(value):
    if isinstance(value,Ref):yield value
    elif isinstance(value,dict):
        for v in value.values():yield from refs(v)
    elif isinstance(value,list):
        for v in value:yield from refs(v)


def summary_value(value):
    if isinstance(value,Ref):return {'ref':[value.number,value.generation]}
    if isinstance(value,bytes):return {'bytes':len(value),'sha256':hashlib.sha256(value).hexdigest()}
    if isinstance(value,dict):return {k:summary_value(v) for k,v in value.items()}
    if isinstance(value,list):return [summary_value(v) for v in value]
    return value


@dataclass
class Record:
    ref: Ref
    start: int
    body_start: int
    end: int
    value: object
    stream_start: int | None = None
    stream_end: int | None = None
    container: int | None = None
    body: bytes | None = None

    def report(self,data):
        return {'object':[self.ref.number,self.ref.generation], 'source_range':[self.start,self.end],
                'type':self.value.get('/Type') if isinstance(self.value,dict) else 'scalar',
                'dictionary':summary_value(self.value),'references':[[r.number,r.generation] for r in refs(self.value)],
                'stream_range':[self.stream_start,self.stream_end] if self.stream_start is not None else None,
                'stream_sha256':hashlib.sha256(data[self.stream_start:self.stream_end]).hexdigest() if self.stream_start is not None else None,
                'object_stream':self.container,'observation':'parsed_source_bytes','validation':'LEXICAL'}


def scan(data):
    records=[];errors=[];cursor=0
    while match:=OBJECT.search(data,cursor):
        if len(records)+len(errors)>=MAX_OBJECTS:raise EvidenceError('Object candidate budget')
        if int(match[1])>MAX_OBJECTS or int(match[2])>65535:raise EvidenceError('Object identifier budget')
        reader=Reader(data,match.end());stream_start=stream_end=None
        try:
            value=reader.value();reader.skip()
            if data[reader.pos:reader.pos+6]==b'stream':
                if not isinstance(value,dict):raise EvidenceError('Stream without dictionary')
                beginning=re.match(rb'stream[ \t]*(?:\r\n|\n|\r)',data[reader.pos:reader.pos+32])
                if not beginning:raise EvidenceError('Stream missing EOL')
                stream_start=reader.pos+beginning.end();length=value.get('/Length')
                if isinstance(length,int) and 0<=length<=len(data)-stream_start:
                    stream_end=stream_start+length;reader.pos=stream_end;reader.skip()
                    if data[reader.pos:reader.pos+9]!=b'endstream':raise EvidenceError('Declared stream length/boundary mismatch')
                elif isinstance(length,Ref):
                    # A later scalar /Length is resolved after inventory. Ambiguous
                    # end markers never authorize scanning inside stream bytes.
                    matches=list(re.finditer(rb'(?:\r\n|\r|\n)endstream\s+endobj\b',data[stream_start:]))
                    if not matches:raise EvidenceError('Indirect stream length has no boundary')
                    stop=matches[0];stream_end=stream_start+stop.start()
                    reader.pos=stream_start+stop.start()+len(stop.group())-len(b'endobj')
                    records.append(Record(Ref(int(match[1]),int(match[2])),match.start(),match.end(),reader.pos+6,value,stream_start,stream_end))
                    cursor=reader.pos+6;continue
                else:raise EvidenceError('Unusable stream Length')
                reader.pos+=9;reader.skip()
            if data[reader.pos:reader.pos+6]!=b'endobj':raise EvidenceError('Missing endobj')
            end=reader.pos+6
            records.append(Record(Ref(int(match[1]),int(match[2])),match.start(),match.end(),end,value,stream_start,stream_end))
            cursor=end
        except EvidenceError as exc:
            errors.append({'offset':match.start(),'reason':str(exc)})
            # Stop at a malformed stream rather than treating its payload as objects.
            if stream_start is not None:break
            cursor=max(match.end(),reader.pos)
    mapping={r.ref:r for r in records}
    for r in records:
        if r.stream_start is not None and isinstance(r.value.get('/Length'),Ref):
            length=mapping.get(r.value['/Length'])
            if not length or type(length.value)!=int or length.value!=r.stream_end-r.stream_start:
                errors.append({'offset':r.start,'reason':'Indirect stream Length mismatch'})
    return records,errors


def decoded_stream(record,data):
    raw=data[record.stream_start:record.stream_end];filters=record.value.get('/Filter',[])
    if isinstance(filters,str):filters=[filters]
    parameters=record.value.get('/DecodeParms')
    if not isinstance(parameters,list):parameters=[parameters]*len(filters)
    if len(parameters)!=len(filters):raise EvidenceError('Filter parameter count')
    for name,params in zip(filters,parameters):
        if name not in ('/FlateDecode','/Fl'):raise EvidenceError('Unsupported structural-stream filter '+name)
        dec=zlib.decompressobj();raw=dec.decompress(raw,MAX_DECODED+1)
        if len(raw)>MAX_DECODED or not dec.eof or dec.unused_data:raise EvidenceError('Structural-stream expansion/boundary limit')
        if params:
            if not isinstance(params,dict):raise EvidenceError('Indirect predictor parameters need resolution')
            predictor=params.get('/Predictor',1);columns=params.get('/Columns',1)
            colors=params.get('/Colors',1);bits=params.get('/BitsPerComponent',8)
            if any(type(v)!=int or v<1 for v in (columns,colors,bits)) or bits not in (1,2,4,8,16):raise EvidenceError('Invalid predictor dimensions')
            rowbytes=(columns*colors*bits+7)//8;bpp=max(1,(colors*bits+7)//8)
            if rowbytes>MAX_DECODED:raise EvidenceError('Predictor row budget')
            if predictor==1:continue
            if predictor==2:
                if bits!=8 or len(raw)%rowbytes:raise EvidenceError('Unsupported TIFF predictor precision/extent')
                out=bytearray(raw)
                for start in range(0,len(out),rowbytes):
                    for i in range(bpp,rowbytes):out[start+i]=(out[start+i]+out[start+i-bpp])&255
                raw=bytes(out);continue
            if not 10<=predictor<=15 or len(raw)%(rowbytes+1):raise EvidenceError('Invalid PNG predictor extent')
            out=bytearray();previous=bytearray(rowbytes)
            for start in range(0,len(raw),rowbytes+1):
                kind=raw[start];row=bytearray(raw[start+1:start+1+rowbytes])
                if kind>4:raise EvidenceError('Invalid PNG row filter')
                for i in range(rowbytes):
                    a=row[i-bpp] if i>=bpp else 0;b=previous[i];c=previous[i-bpp] if i>=bpp else 0
                    if kind==0:v=0
                    elif kind==1:v=a
                    elif kind==2:v=b
                    elif kind==3:v=(a+b)//2
                    else:
                        p=a+b-c;distances=(abs(p-a),abs(p-b),abs(p-c))
                        v=(a,b,c)[distances.index(min(distances))]
                    row[i]=(row[i]+v)&255
                out+=row;previous=row
            raw=bytes(out)
    if len(raw)>MAX_DECODED:raise EvidenceError('Decoded stream limit')
    return raw


def expand_objects(records,data):
    expanded=[]
    for record in records:
        if not isinstance(record.value,dict) or record.value.get('/Type')!='/ObjStm':continue
        raw=decoded_stream(record,data);n=record.value.get('/N');first=record.value.get('/First')
        if type(n)!=int or not 0<n<=MAX_OBJECTS or type(first)!=int or not 0<first<=len(raw):raise EvidenceError('Invalid ObjStm index')
        header=raw[:first].split()
        if len(header)!=2*n or not all(t.isdigit() for t in header):raise EvidenceError('ObjStm header count')
        pairs=[(int(header[i]),int(header[i+1])) for i in range(0,len(header),2)]
        if len(set(num for num,_ in pairs))!=n:raise EvidenceError('Duplicate compressed object')
        for i,(number,offset) in enumerate(pairs):
            end=first+(pairs[i+1][1] if i+1<n else len(raw)-first);start=first+offset
            if not 0<=number<=MAX_OBJECTS or not first<=start<end<=len(raw):raise EvidenceError('ObjStm extent/order')
            reader=Reader(raw,start,end);value=reader.value();reader.skip()
            if reader.pos!=end:raise EvidenceError('Trailing compressed object bytes')
            expanded.append(Record(Ref(number),record.start,record.start,record.end,value,container=record.ref.number,body=raw[start:end]))
            if len(expanded)+len(records)>MAX_OBJECTS:raise EvidenceError('Expanded object budget')
    return expanded


def references(records,data):
    tables=[]
    for r in records:
        if not isinstance(r.value,dict) or r.value.get('/Type')!='/XRef':continue
        value=r.value;w=value.get('/W');size=value.get('/Size');index=value.get('/Index',[0,size])
        if not isinstance(w,list) or len(w)!=3 or any(type(i)!=int or not 0<=i<=8 for i in w) or sum(w)==0:raise EvidenceError('Invalid XRef /W')
        if type(size)!=int or not 0<size<=MAX_OBJECTS or not isinstance(index,list) or len(index)%2:raise EvidenceError('Invalid XRef index')
        raw=decoded_stream(r,data);cursor=0;entries={}
        for start,count in zip(index[::2],index[1::2]):
            if type(start)!=int or type(count)!=int or not 0<=start<=start+count<=size:raise EvidenceError('XRef index range')
            for n in range(start,start+count):
                if n in entries or cursor+sum(w)>len(raw):raise EvidenceError('Duplicate/truncated XRef entry')
                fields=[]
                for width in w:fields.append(int.from_bytes(raw[cursor:cursor+width],'big'));cursor+=width
                if not w[0]:fields[0]=1
                if fields[0] not in (0,1,2):raise EvidenceError('Unknown XRef entry type')
                entries[n]=fields
        if cursor!=len(raw):raise EvidenceError('XRef decoded length mismatch')
        tables.append({'offset':r.start,'kind':'stream','trailer':value,'entries':entries})
    # Only top-level classic xrefs, never literal xref bytes in a stream.
    from ..parser import classic_xref
    from ..models import Unsupported
    for match in re.finditer(rb'(?m)(?<![a-z])xref\s',data):
        if any(r.start<=match.start()<r.end for r in records):continue
        try:
            rows,end=classic_xref(data,match.start());reader=Reader(data,end+7);trailer=reader.value()
            tables.append({'offset':match.start(),'kind':'classic','trailer':trailer,
                           'entries':{e.number:[1,e.offset,e.generation] for e in rows}})
        except (Unsupported,EvidenceError):continue
    return tables


def collect(data):
    records,errors=scan(data);tables=references(records,data);expanded=expand_objects(records,data)
    positions={}
    for r in records:positions.setdefault(r.ref,[]).append(r.start)
    from collections import Counter
    votes=Counter()
    for table in tables:
        for n,(kind,a,b) in table['entries'].items():
            if kind==1:
                for pos in positions.get(Ref(n,b),[]):votes[pos-a]+=1
    shifts=[{'origin':shift,'relationships':count,'observation':'stored_vs_actual_object_offsets'} for shift,count in votes.most_common(8)]
    report={'schema':1,'observations':{'headers':[m.start() for m in re.finditer(rb'%PDF-\d\.\d',data) if not any(r.start<=m.start()<r.end for r in records)],
          'startxref':[{'offset':m.start(),'value':int(m[1])} for m in re.finditer(rb'startxref\s+(\d+)\s+%%EOF',data)],
          'objects':len(records),'compressed_objects':len(expanded),'streams':sum(r.stream_start is not None for r in records),
          'images':sum(isinstance(r.value,dict) and r.value.get('/Subtype')=='/Image' for r in records),
          'reference_tables':[{'offset':t['offset'],'style':t['kind'],'entries':len(t['entries']),'trailer':summary_value(t['trailer'])} for t in tables]},
          'coordinate_hypotheses':shifts or [{'origin':'NO_SINGLE_SHIFT','relationships':0}],
          'scanner_errors':errors,'object_map':[r.report(data) for r in records+expanded]}
    return records,expanded,tables,report
