"""Shared, serializable record of what a repair did."""
from dataclasses import dataclass, field, asdict
from .. import __version__

SCHEMA = 1
GENERIC_CLASSES = {'DECODED_IMAGE_CONTENT','DECODED_PDF_CONTENT','STRUCTURAL_PDF_RECOVERY','PAGE_LEVEL_RECOVERY','IMAGE_LEVEL_RECOVERY'}


@dataclass
class RecoveredDocument:
    source_family: str
    recovery_level: str
    pages: list
    resources: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)
    confidence: str = 'VALIDATED_STRUCTURE'
    warnings: list = field(default_factory=list)
    provenance: dict = field(default_factory=dict)
    assumptions: list = field(default_factory=list)
    validation: dict = field(default_factory=dict)
    schema: int = SCHEMA
    engine_version: str = __version__

    def to_dict(self):return asdict(self)


def from_result(result):
    levels={'STRICT_EXACT_RECOVERY':'EXACT',
            'DECODED_PDF_CONTENT':'DECODED','STRUCTURAL_PDF_RECOVERY':'STRUCTURAL_REPAIR',
            'PAGE_LEVEL_RECOVERY':'PAGE_LEVEL_RECOVERY','IMAGE_LEVEL_RECOVERY':'IMAGE_LEVEL_RECOVERY'}
    old=result.recovered_document
    if old:
        old.update(warnings=[result.warning] if result.warning else [],assumptions=result.assumptions)
        old['validation']={**old.get('validation',{}),'status':result.validation_status,'mode':result.validation_mode}
        return old
    return RecoveredDocument(source_family=result.dimensions.get('FORMAT_IDENTIFIED','PDF'),
        recovery_level=levels.get(result.recovery_class,'UNSUPPORTED'),
        pages=[{'number':i+1} for i in range(result.page_count)],
        warnings=[result.warning] if result.warning else [],assumptions=result.assumptions,
        provenance={'source_sha256':result.source_hash,'strategy':result.conversion_strategy,'preservation':result.preservation},
        validation={'status':result.validation_status,'mode':result.validation_mode}).to_dict()


def selected_model(model,pages,*,printed=False):
    import copy
    value=copy.deepcopy(model);original=value['pages']
    value['pages']=[{**original[p-1],'number':i+1,'source_page':original[p-1].get('source_page',p)} for i,p in enumerate(pages)]
    for index,page in enumerate(value['pages']):
        if 'page_index' in page:page['page_index']=index
    supplemental=value.get('metadata',{}).get('supplemental_pages')
    if supplemental is not None:
        positions={number:index for index,number in enumerate(pages)}
        value['metadata']['supplemental_pages']=[{**page,'number':positions[page['number']]+1,
            'page_index':positions[page['number']],'source_page':page.get('source_page',page['number'])}
            for page in supplemental if page['number'] in positions]
    value['provenance']['selection']=pages
    value['provenance']['document_relation']='PRINT_RASTERIZED' if printed else 'PAGE_SUBSET_REBUILT'
    value['confidence']='PAGE_STREAMS_PRESERVED'
    value['provenance']['full_component_bytes_preserved']=False
    value['validation']={'status':'PENDING','mode':'all_selected_pages',
                         'source_document_status':model.get('validation',{}).get('source_document_status',model.get('validation',{}).get('status','UNKNOWN'))}
    value['resources']={'source_resources':value['resources'],'selected_page_streams':'VERIFIED_BY_EXPORT'}
    if printed:
        value['confidence']='PRINT_RENDERING'
        value['provenance']['rendering_mode']='Qt-print-to-PDF'
        value['resources']={'original_streams_preserved':False}
    return value
