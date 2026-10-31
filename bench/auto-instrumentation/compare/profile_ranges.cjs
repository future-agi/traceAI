// Read-only attribution of minified keepNames bundle positions; never rewrite it.
const fs = require('node:fs');
const acorn = require(process.argv[2] + '/node_modules/acorn');
const source = fs.readFileSync(process.argv[3],'utf8');
const ast = acorn.parse(source,{ecmaVersion:'latest',sourceType:'script'});
const labelCandidates = new Map();
const labels = new Map();
function children(node) {
  return Object.entries(node).flatMap(([k,v])=> k==='parent'?[]:Array.isArray(v)?v.filter(x=>x?.type):v?.type?[v]:[]);
}
function scan(node) {
  if(node.type==='CallExpression' && node.callee.name==='a' && node.arguments[0]?.type==='Identifier' && typeof node.arguments[1]?.value==='string') {
    const key=node.arguments[0].name;
    if(!labelCandidates.has(key)) labelCandidates.set(key,new Set());
    labelCandidates.get(key).add(node.arguments[1].value);
  }
  for(const child of children(node)) scan(child);
}
scan(ast);
// A minified local identifier (e.g. `e`) is reused in unrelated lexical scopes.
// Never relabel it using another scope's keepNames call. Ambiguities abstain.
for(const [key,values] of labelCandidates) if(values.size===1) labels.set(key,[...values][0]);
const groups = {
 privacy_S1:new Set(['observeOutgoing','observeRequest','observeHeaders','observeTransports','registerSecret','registerSecrets','getRegisteredSecrets','discoverSecrets','seedSecrets','retainBodySecrets','captureSecrets']),
 privacy_S5_S6:new Set(['privacyFilter','scrubValue','scrubAttribute','scrubURL','scrubString']),
 attributes:new Set(['build','checkRequest','match','resolveProvider','putMessages','openaiMessage','respOpenaiChat','reqOpenaiChat','respOpenaiEmbeddings','requestData']),
 sse_reassembly:new Set(['parseSSE','parseSse','parseNDJSON','parseNdjson']),
 body_copy:new Set(['encodeBytes','decodeBytes','concatBytes','byteLength']),
 transport_buffer:new Set(['createFetchHook','observeMethod','observePromise','preparePromiseObservation','decodeResponse','decodeNodeBody','responseEncoding','headersOf','patchFetch']),
 context:new Set(['shouldSkip','trackCoverage','AsyncLocalStorageContextManager','ContextAPI','AsyncHooksContextManager','getAttributesFromContext']),
 span:new Set(['FITracer','FISpan','Tracer','SpanImpl','Span','maskSpan']),
 processor_export:new Set(['SimpleSpanProcessor','BatchSpanProcessor','BatchSpanProcessorBase','MultiSpanProcessor','guardedExporter','OTLPTraceExporter','OTLPExportDelegate','OTLPExporterBase','ProtobufTraceSerializer','JsonTraceSerializer'])
};
const ours = new Set([...Object.values(groups).flatMap(s=>[...s])].filter(x=>!['Tracer','SpanImpl','Span','SimpleSpanProcessor','BatchSpanProcessor','BatchSpanProcessorBase','MultiSpanProcessor','OTLPTraceExporter','OTLPExportDelegate','OTLPExporterBase','ProtobufTraceSerializer','JsonTraceSerializer','AsyncLocalStorageContextManager','ContextAPI','AsyncHooksContextManager'].includes(x)));
const ranges = [];
function walk(node,stage=null,owner=false,parent=null) {
  let raw = node.id?.name;
  if(parent?.type==='VariableDeclarator' && parent.init===node) raw=parent.id?.name??raw;
  if(parent?.type==='AssignmentExpression' && parent.right===node) raw=parent.left?.name??raw;
  let name=labels.get(raw)??raw;
  if(!name && node.type==='MethodDefinition') name=node.key?.name;
  const found = name && Object.entries(groups).find(([,names])=>names.has(name));
  if(found) { stage=found[0]; owner=ours.has(name); }
  if(node.type==='FunctionDeclaration' || node.type==='FunctionExpression' || node.type==='ArrowFunctionExpression' || node.type==='MethodDefinition' || node.type==='ClassExpression' || node.type==='ClassDeclaration') {
    if(stage) ranges.push({start:node.start,end:node.end,stage,ours:owner,name:name??null});
  }
  for(const child of children(node)) walk(child,stage,owner,node);
}
walk(ast);
console.log(JSON.stringify({ranges,line_starts:[0,...[...source.matchAll(/\n/g)].map(m=>m.index+1)],labels:Object.fromEntries(labels)}));
