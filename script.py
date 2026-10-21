# -*- coding: utf-8 -*-
"""Revit 2026 / pyRevit V5.3: read-only Stratus/Autodesk translation diagnostic.
Runs inside an OPEN PROJECT. No transactions, no model changes, no uploads.
Compatible with pyRevit IronPython 2.7 / CPython 3 (with Revit API available).
"""
from __future__ import print_function
import os, sys, csv, datetime, unicodedata, traceback, math
from collections import defaultdict
from Autodesk.Revit.DB import (FilteredElementCollector, Family, FamilySymbol,
    FamilyInstance, ImportInstance, RevitLinkInstance, Options, ViewDetailLevel,
    GeometryInstance, Solid, Mesh, NamingUtils, ModelPathUtils, Element, BuiltInParameter,
    DirectShape, Wall, Floor, Ceiling, RoofBase, Group, AssemblyInstance,
    View3D, ViewType, StorageType, RevitLinkType, BuiltInCategory)
# The Fabrication namespace may not be resolvable in all IronPython hosts.
# Fabrication sampling does not depend on importing a .NET class.
try:
    from Autodesk.Revit.DB.Fabrication import FabricationPart
except Exception:
    FabricationPart = None

# SETTINGS: deep scan opens independent family documents and closes them; slower.
DEEP_FAMILY_SCAN = False
ASK_SCAN_MODE = True
# Number of placed examples per family to probe for geometry extraction errors.
GEOMETRY_SAMPLES_PER_FAMILY = 2
# If disabled, zero geometry extraction API calls are made.
CHECK_INSTANCE_GEOMETRY = True
# If deep scan enabled, inspect family-contained CAD imports and nested loaded families.
MAX_DEEP_FAMILIES = 10000
# A broad non-family inventory without calling geometry on every element.
SAMPLE_SYSTEM_GEOMETRY_PER_CATEGORY = 8
SAMPLE_DIRECTSHAPES = 40
# Fabrication geometry probes can be expensive: start small, then increase.
FABRICATION_SCAN_LIMIT = 250
FABRICATION_MODE = "SAMPLE"
FABRICATION_BATCH_SIZE = 250
# When true, creates one element-identity row per sampled geometry test.
INCLUDE_SAMPLED_ELEMENTS = True

try:
    from pyrevit import revit, script
    doc = revit.doc
    output = script.get_output()
except ImportError:
    doc = __revit__.ActiveUIDocument.Document
    output = None

try:
    text_type = unicode
except NameError:
    text_type = str

def txt(obj):
    if obj is None: return u''
    try: return text_type(obj)
    except Exception: return u'<unprintable>'

def idstr(obj):
    try: return txt(obj.Id.Value)
    except Exception:
        try: return txt(obj.Id.IntegerValue)
        except Exception: return ''

def element_name(el):
    """Revit element name without IronPython FamilySymbol.Name binder failure."""
    if el is None: return u''
    try:
        return txt(Element.Name.GetValue(el))
    except Exception:
        pass
    try:
        return txt(el.Name)
    except Exception:
        pass
    for parameter in (BuiltInParameter.SYMBOL_NAME_PARAM, BuiltInParameter.ALL_MODEL_TYPE_NAME):
        try:
            p = el.get_Parameter(parameter)
            if p:
                val = p.AsString()
                if val is not None: return txt(val)
        except Exception:
            pass
    return u'<NAME_UNAVAILABLE>'

def catname(el):
    try: return txt(el.Category.Name) if el.Category else ''
    except Exception: return ''

def safe_uid(el):
    try: return txt(el.UniqueId)
    except Exception: return ''

def typename(el):
    try:
        e = doc.GetElement(el.GetTypeId())
        return element_name(e) if e else ''
    except Exception: return ''

def viewinfo(el):
    try:
        view = doc.GetElement(el.OwnerViewId)
        return element_name(view) if view else ''
    except Exception: return ''

def worksetinfo(el):
    try:
        ws = doc.GetWorksetTable().GetWorkset(el.WorksetId)
        return txt(ws.Name) if ws else ''
    except Exception: return ''

def phaseinfo(el):
    try:
        phase = doc.GetElement(el.CreatedPhaseId)
        return element_name(phase) if phase else ''
    except Exception: return ''

def bboxinfo(el):
    try:
        bb = el.get_BoundingBox(None)
        if bb is None: return 'NO_BBOX'
        values = [bb.Min.X,bb.Min.Y,bb.Min.Z,bb.Max.X,bb.Max.Y,bb.Max.Z]
        return ','.join(['%.3f' % v for v in values])
    except Exception as ex: return 'EXCEPTION: '+errmsg(ex)

def errmsg(ex): return (txt(type(ex).__name__) + ': ' + txt(ex))[:700]

# Name heuristics are evidence, NOT Autodesk translation validation.
# Slash and inch marks are common in legitimate MEP type names.
WINDOWS_UNSAFE = set(u'\\/:*?"<>|')
COMMON_MEP_PUNCT = set(u'\\/:*?"<>|#&+%@')

def check_name(value):
    n = txt(value)
    flags, chars = [], []
    try:
        if not NamingUtils.IsValidName(n):
            flags.append('REVIT_INVALID_NAME')
    except Exception as ex:
        flags.append('REVIT_NAME_CHECK_UNAVAILABLE')
    if not n or n != n.strip():
        flags.append('EMPTY_OR_EDGE_WHITESPACE')
    for ch in n:
        k = unicodedata.category(ch)
        cp = ord(ch)
        if ch == u'\ufffd' or k in ('Cc','Cf','Cs','Co','Cn'):
            issue = 'HIDDEN_OR_CONTROL_UNICODE'
        elif cp > 127:
            issue = 'NON_ASCII_REVIEW'
        elif ch in COMMON_MEP_PUNCT:
            issue = 'COMMON_MECHANICAL_PUNCTUATION'
        else:
            continue
        if issue not in flags: flags.append(issue)
        chars.append(u'U+%04X %s [%s]' % (cp, txt(unicodedata.name(ch, 'UNKNOWN')), issue))
    if 'REVIT_INVALID_NAME' in flags or 'HIDDEN_OR_CONTROL_UNICODE' in flags:
        priority = 'HIGH'
    elif 'EMPTY_OR_EDGE_WHITESPACE' in flags or 'NON_ASCII_REVIEW' in flags:
        priority = 'REVIEW'
    else:
        priority = 'OK'
    return priority, '; '.join(flags), '; '.join(chars)

def issue_record(severity, scope, family, typ, element_id, details, evidence='HEURISTIC', action='Review in Revit'):
    return {'Severity':severity,'Scope':scope,'Family':family,'Type':typ,
            'Element ID':element_id,'Details':details,'Evidence':evidence,'Recommended Action':action}

def geometry_probe(el):
    """API extraction test, NOT a validation of Autodesk cloud translation."""
    try:
        opt = Options()
        opt.DetailLevel = ViewDetailLevel.Medium
        opt.IncludeNonVisibleObjects = False
        geom = el.get_Geometry(opt)
        if geom is None: return ('NO_GEOMETRY_RETURNED', 'geometry was null; may be expected')
        total, solids, meshes, nested = 0, 0, 0, 0
        for go in geom:
            total += 1
            if isinstance(go, Solid): solids += 1
            elif isinstance(go, Mesh): meshes += 1
            elif isinstance(go, GeometryInstance): nested += 1
        return ('OK', 'objects=%s solids=%s meshes=%s geometry_instances=%s' % (total,solids,meshes,nested))
    except Exception as ex:
        return ('EXCEPTION', errmsg(ex))

# V5.2 recursive geometry inspection. Local API inspection, NOT cloud validation.
# Guard depth and total objects to avoid long-running malformed geometry traversal.
DEEP_GEOMETRY_MAX_DEPTH = 12
DEEP_GEOMETRY_MAX_OBJECTS = 20000

def deep_geometry_probe(el):
    counts = {'objects':0,'instances':0,'solids':0,'faces':0,'edges':0,
              'meshes':0,'triangles':0,'positive_solids':0,'zero_volume_solids':0,
              'max_depth':0,'truncated':0}
    errors=[]
    volume=0.0
    try:
        options=Options()
        options.DetailLevel=ViewDetailLevel.Medium
        options.IncludeNonVisibleObjects=False
        geom=el.get_Geometry(options)
        if geom is None:
            return ('NO_GEOMETRY_RETURNED','null top-level geometry',counts)
        def visit(collection, depth):
            nonlocal_dummy = None  # Works on IronPython 2.7: use mutable counts instead of nonlocal.
            if depth>DEEP_GEOMETRY_MAX_DEPTH:
                counts['truncated']+=1
                return
            if depth>counts['max_depth']: counts['max_depth']=depth
            try:
                for item in collection:
                    if counts['objects']>=DEEP_GEOMETRY_MAX_OBJECTS:
                        counts['truncated']+=1
                        return
                    counts['objects']+=1
                    if isinstance(item,GeometryInstance):
                        counts['instances']+=1
                        try:
                            visit(item.GetInstanceGeometry(),depth+1)
                        except Exception as exc:
                            errors.append('depth=%s instance: %s' % (depth,errmsg(exc)))
                    elif isinstance(item,Solid):
                        counts['solids']+=1
                        try:
                            v=float(item.Volume)
                            if v>1e-9: counts['positive_solids']+=1
                            else: counts['zero_volume_solids']+=1
                            counts.setdefault('volume_ft3',0.0)
                            counts['volume_ft3']+=v
                        except Exception as exc:
                            errors.append('solid volume: '+errmsg(exc))
                        try: counts['faces']+=item.Faces.Size
                        except Exception as exc: errors.append('solid faces: '+errmsg(exc))
                        try: counts['edges']+=item.Edges.Size
                        except Exception as exc: errors.append('solid edges: '+errmsg(exc))
                    elif isinstance(item,Mesh):
                        counts['meshes']+=1
                        try: counts['triangles']+=item.NumTriangles
                        except Exception as exc: errors.append('mesh triangles: '+errmsg(exc))
            except Exception as exc:
                errors.append('iterate geometry at depth %s: %s' % (depth,errmsg(exc)))
        visit(geom,0)
        if errors: status='NESTED_GEOMETRY_EXCEPTION'
        elif counts['truncated']: status='TRUNCATED'
        else: status='OK'
        detail='objects=%s instances=%s solids=%s faces=%s edges=%s meshes=%s triangles=%s volume_ft3=%.6f depth=%s truncated=%s' % (
            counts['objects'],counts['instances'],counts['solids'],counts['faces'],counts['edges'],
            counts['meshes'],counts['triangles'],counts.get('volume_ft3',0),counts['max_depth'],counts['truncated'])
        if errors: detail+=' | ERRORS: '+'; '.join(errors[:5])
        return (status,detail,counts)
    except Exception as exc:
        return ('EXCEPTION',errmsg(exc),counts)

def fabrication_parameter_fallback(el, candidates):
    for name in candidates:
        try:
            p=el.LookupParameter(name)
            if p is None: continue
            value=p.AsString() or p.AsValueString()
            if value: return txt(value)
        except Exception: pass
    return ''

def export_csv(path, fields, rows):
    # Python 2 csv wants bytes, Python 3 wants text.
    if sys.version_info[0] < 3:
        with open(path, 'wb') as f:
            w = csv.writer(f)
            w.writerow([txt(x).encode('utf-8') for x in fields])
            for row in rows: w.writerow([txt(row.get(k, '')).encode('utf-8') for k in fields])
    else:
        with open(path, 'w', newline='', encoding='utf-8-sig') as f:
            w = csv.writer(f)
            w.writerow(fields)
            for row in rows: w.writerow([txt(row.get(k, '')) for k in fields])

if doc is None or doc.IsFamilyDocument:
    raise Exception('Open the Revit project (.rvt) rather than a family (.rfa) before running.')

# Mode selector (pyRevit IronPython-compatible). Cancel exits with no scan.
if ASK_SCAN_MODE:
    try:
        from pyrevit import forms
        choice = forms.CommandSwitchWindow.show(
            ['Standard scan (fast)', 'Deep scan (opens editable families)'],
            message='Stratus / Autodesk translation diagnostic — scan mode')
        if not choice:
            raise SystemExit
        DEEP_FAMILY_SCAN = choice.startswith('Deep')
    except Exception as ex:
        # In unsupported hosts, use DEEP_FAMILY_SCAN setting above.
        print('Scan mode picker unavailable; using DEEP_FAMILY_SCAN=%s: %s' % (DEEP_FAMILY_SCAN, errmsg(ex)))

# Select fabrication scan size separately from the existing family Deep Scan.
# SAMPLE is deterministic and distributed across services/CIDs, not just first rows.
try:
    from pyrevit import forms
    fab_choice = forms.CommandSwitchWindow.show(
        ['Sample 250 fabrication parts (recommended)',
         'Sample 1500 fabrication parts',
         'Scan all fabrication parts (slow)'],
        message='V5.2 - choose fabrication geometry scan coverage')
    if not fab_choice:
        raise SystemExit
    if fab_choice.startswith('Sample 1500'):
        FABRICATION_SCAN_LIMIT = 1500
    elif fab_choice.startswith('Scan all'):
        FABRICATION_SCAN_LIMIT = -1
except ImportError:
    pass

started = datetime.datetime.now()
folder = os.path.join(os.path.expanduser('~'), 'Desktop', 'Revit_Stratus_Diagnostics')
if not os.path.isdir(os.path.dirname(folder)):
    folder = os.path.join(os.path.expanduser('~'), 'Documents', 'Revit_Stratus_Diagnostics')
if not os.path.isdir(folder): os.makedirs(folder)
model = txt(doc.Title)
stamp = started.strftime('%Y%m%d_%H%M%S')
base = 'StratusDiagnostic_' + stamp

families = list(FilteredElementCollector(doc).OfClass(Family))
symbols = list(FilteredElementCollector(doc).OfClass(FamilySymbol))
instances = list(FilteredElementCollector(doc).OfClass(FamilyInstance).WhereElementIsNotElementType())
family_by_id = {idstr(f):f for f in families}
family_by_symbol = {}
family_symbols = defaultdict(list)
for sym in symbols:
    try:
        fid = idstr(sym.Family)
        family_by_symbol[idstr(sym)] = fid
        family_symbols[fid].append(sym)
    except Exception: pass

counts = defaultdict(int)
family_instances = defaultdict(list)
nested_children = defaultdict(set)
for inst in instances:
    try:
        sid = idstr(inst.Symbol)
        fid = family_by_symbol.get(sid, '')
        counts[sid] += 1
        if fid:
            family_instances[fid].append(inst)
            for sub_id in inst.GetSubComponentIds():
                try:
                    sub = doc.GetElement(sub_id)
                    if isinstance(sub, FamilyInstance):
                        nested_children[fid].add(element_name(sub.Symbol.Family))
                except Exception: pass
    except Exception: pass

# Mark family name collisions after Unicode case-fold approximation.
name_index = defaultdict(list)
for f in families:
    try: name_index[unicodedata.normalize('NFKC',element_name(f)).lower()].append(idstr(f))
    except Exception: pass

family_rows, type_rows, issue_rows, import_rows = [], [], [], []
element_rows, category_rows, isolation_rows = [], [], []
for f in families:
    fid = idstr(f)
    fn = element_name(f)
    priority, flags, characters = check_name(fn)
    try: editable = bool(f.IsEditable)
    except Exception: editable = False
    try: inplace = bool(f.IsInPlace)
    except Exception: inplace = False
    syms = family_symbols.get(fid, [])
    placed = len(family_instances.get(fid, []))
    duplicate = len(name_index.get(unicodedata.normalize('NFKC',fn).lower(), [])) > 1
    if duplicate:
        flags = (flags + '; ' if flags else '') + 'NORMALIZED_NAME_COLLISION'
        if priority == 'OK': priority = 'REVIEW'
    rec = {'Priority':priority,'Family Name':fn,'Category':catname(f),'Family Element ID':fid,
           'Family Unique ID':txt(f.UniqueId),'Types':len(syms),'Placed Instances':placed,
           'Editable':editable,'In Place':inplace,'Name Flags':flags,'Unicode Characters':characters,
           'Nested Shared Subfamilies (placed)':'; '.join(sorted(nested_children.get(fid,set()))),
           'Geometry Probe':'NOT_TESTED','Geometry Detail':'',
           'Family Open Test':'NOT_TESTED','Family Internal CAD Imports':'NOT_TESTED',
           'Nested Families (family editor)':'NOT_TESTED','Deep Scan Details':''}
    if CHECK_INSTANCE_GEOMETRY and placed:
        outcomes = []
        for inst in family_instances[fid][:GEOMETRY_SAMPLES_PER_FAMILY]:
            st, det = geometry_probe(inst)
            outcomes.append('%s: %s (%s)' % (idstr(inst),st,det))
        rec['Geometry Probe'] = ('EXCEPTION' if any('EXCEPTION' in x for x in outcomes)
             else 'NO_GEOMETRY_RETURNED' if any('NO_GEOMETRY_RETURNED' in x for x in outcomes) else 'OK')
        rec['Geometry Detail'] = ' | '.join(outcomes)
        if rec['Geometry Probe'] == 'EXCEPTION':
            rec['Priority'] = 'HIGH'
    family_rows.append(rec)

    for sym in syms:
        tp, tf, tc = check_name(element_name(sym))
        trow = {'Priority':tp,'Family Name':fn,'Type Name':element_name(sym),
                'Category':catname(sym),'Family Element ID':fid,'Type Element ID':idstr(sym),
                'Type Unique ID':txt(sym.UniqueId),'Placed Instances':counts.get(idstr(sym),0),
                'Name Flags':tf,'Unicode Characters':tc}
        type_rows.append(trow)
        if tp != 'OK':
            issue_rows.append(issue_record(tp,'TYPE NAME',fn,element_name(sym),idstr(sym), tf + ' '+tc,
                'NAME_HEURISTIC', 'Inspect hidden Unicode or actual Revit naming violations'))
    if rec['Priority'] != 'OK':
        issue_rows.append(issue_record(rec['Priority'],'FAMILY NAME / GEOMETRY',fn,'',fid,
            rec['Name Flags']+'; '+rec['Geometry Detail'], 'API_EXCEPTION' if rec['Geometry Probe']=='EXCEPTION' else 'NAME_HEURISTIC',
            'Inspect sampled geometry and Unicode; investigate any actual exceptions'))

# Imported/link CAD in the project (family editor imports are scanned optionally).
for imp in FilteredElementCollector(doc).OfClass(ImportInstance):
    try:
        linked = bool(imp.IsLinked)
        typ = doc.GetElement(imp.GetTypeId())
        import_rows.append({'Location':'PROJECT','Family Name':'','Element ID':idstr(imp),
                            'Name':element_name(typ if typ else imp),'Kind':'LINKED_CAD' if linked else 'IMPORTED_CAD'})
    except Exception as ex:
        issue_rows.append({'Severity':'REVIEW','Scope':'CAD INSPECTION','Family':'','Type':'',
                           'Element ID':idstr(imp),'Details':errmsg(ex)})
for link in FilteredElementCollector(doc).OfClass(RevitLinkInstance):
    import_rows.append({'Location':'PROJECT','Family Name':'','Element ID':idstr(link),
                        'Name':element_name(link),'Kind':'REVIT_LINK'})

if DEEP_FAMILY_SCAN:
    # EditFamily creates an independent editable copy. We CLOSE it without saving.
    # Must never be run while the project document is modifiable or read-only.
    if doc.IsModifiable or doc.IsReadOnly:
        issue_rows.append({'Severity':'REVIEW','Scope':'DEEP SCAN','Family':'','Type':'','Element ID':'',
                           'Details':'Skipped: project IsModifiable or IsReadOnly.'})
    else:
        checked = 0
        for rec in family_rows:
            if checked >= MAX_DEEP_FAMILIES: break
            f = family_by_id.get(rec['Family Element ID'])
            if not rec['Editable'] or rec['In Place']:
                rec['Family Open Test'] = 'SKIPPED_NOT_EDITABLE'
                continue
            checked += 1
            fdoc = None
            try:
                fdoc = doc.EditFamily(f)
                rec['Family Open Test'] = 'OK'
                embedded = list(FilteredElementCollector(fdoc).OfClass(ImportInstance))
                rec['Family Internal CAD Imports'] = len(embedded)
                # Count only actual PLACED nested FamilyInstance elements.
                # Listing Family definitions returns unrelated template annotation families.
                nested_actual = []
                for ni in FilteredElementCollector(fdoc).OfClass(FamilyInstance).WhereElementIsNotElementType():
                    try:
                        nested_actual.append(element_name(ni.Symbol.Family) + ' [id=' + idstr(ni) + ']')
                    except Exception:
                        nested_actual.append('<UNKNOWN> [id=' + idstr(ni) + ']')
                rec['Nested Families (family editor)'] = '; '.join(nested_actual)[:3000]
                for imp in embedded:
                    import_rows.append({'Location':'FAMILY','Family Name':rec['Family Name'],
                                        'Element ID':idstr(imp),'Name':element_name(imp),
                                        'Kind':'LINKED_CAD' if imp.IsLinked else 'IMPORTED_CAD'})
                if embedded:
                    rec['Priority'] = 'REVIEW' if rec['Priority']=='OK' else rec['Priority']
            except Exception as ex:
                rec['Family Open Test'] = 'EXCEPTION'
                rec['Deep Scan Details'] = errmsg(ex)
                rec['Priority'] = 'HIGH'
                issue_rows.append(issue_record('HIGH','FAMILY OPEN EXCEPTION',rec['Family Name'], '',
                    rec['Family Element ID'], errmsg(ex), 'API_EXCEPTION', 'Inspect family in editor; provide to Autodesk support'))
            finally:
                if fdoc is not None:
                    try: fdoc.Close(False)
                    except Exception as ex:
                        issue_rows.append({'Severity':'REVIEW','Scope':'FAMILY CLOSE','Family':rec['Family Name'],
                                           'Type':'','Element ID':rec['Family Element ID'],'Details':errmsg(ex)})

# Include deep-scan revisions in issues file (no duplicates for exact IDs / scopes).
for r in family_rows:
    if r['Family Open Test'] == 'EXCEPTION': continue
    if r['Family Internal CAD Imports'] not in ('NOT_TESTED', 0):
        issue_rows.append(issue_record('REVIEW','FAMILY CAD IMPORT',r['Family Name'],'', r['Family Element ID'],
            'Imported/linked CAD elements inside family: '+txt(r['Family Internal CAD Imports']),
            'API_OBSERVATION','Inspect import geometry; do not assume it causes translation failure'))

# ---- V4: project element coverage beyond loadable families ----
# FamilyInstance geometry has been sampled above. System elements, DirectShapes,
# groups, assemblies and imports are inventoried separately. NO elements are deleted.

def add_element_row(el, scope, sample_geometry=False, note=''):
    en = element_name(el)
    st, det = ('NOT_TESTED', '')
    if sample_geometry:
        st, det = geometry_probe(el)
    entry = {'Scope':scope, 'Category':catname(el), 'Element Class':txt(el.GetType().Name),
             'Element ID':idstr(el), 'Unique ID':safe_uid(el), 'Name':en,
             'Type Name':typename(el), 'Workset':worksetinfo(el), 'Phase':phaseinfo(el),
             'Owner View':viewinfo(el), 'Bounding Box (feet)':bboxinfo(el),
             'Geometry Probe':st, 'Geometry Detail':det, 'Note':note}
    element_rows.append(entry)
    if st == 'EXCEPTION':
        issue_rows.append(issue_record('HIGH', 'ELEMENT GEOMETRY EXCEPTION', en,
            entry['Type Name'], entry['Element ID'], det, 'API_EXCEPTION',
            'Locate the element by ID; inspect it in a disposable copy'))
    return entry

# Category inventory covers project instance elements, not just component families.
# It does not guarantee coverage of non-geometric internal objects.
all_instances = list(FilteredElementCollector(doc).WhereElementIsNotElementType())
category_buckets = defaultdict(list)
for el in all_instances:
    cn = catname(el)
    if cn:
        category_buckets[cn].append(el)
for cname in sorted(category_buckets.keys()):
    els = category_buckets[cname]
    kinds = defaultdict(int)
    for el in els:
        try: kinds[txt(el.GetType().Name)] += 1
        except Exception: kinds['UNKNOWN'] += 1
    category_rows.append({'Category':cname, 'Element Count':len(els),
        'Element Classes':'; '.join('%s: %s' % (k,v) for k,v in sorted(kinds.items())),
        'Sampled Count':0, 'Exceptions':0,
        'Notes':'Project instances only; API geometry sampling is selective'})

# The buckets here are meant for targeted isolate-by-category tests, not automatic edits.
tracked_system = set(['Wall','Floor','Ceiling','RoofBase'])
for row in category_rows:
    cname = row['Category']
    bucket = category_buckets[cname]
    candidates = []
    for el in bucket:
        try:
            if txt(el.GetType().Name) in tracked_system:
                candidates.append(el)
        except Exception: pass
    if not candidates: continue
    selected = candidates[:SAMPLE_SYSTEM_GEOMETRY_PER_CATEGORY]
    row['Sampled Count'] = len(selected)
    for el in selected:
        er = add_element_row(el, 'SYSTEM_ELEMENT', True, 'Selective system-element sample')
        if er['Geometry Probe'] == 'EXCEPTION': row['Exceptions'] += 1

shapes = list(FilteredElementCollector(doc).OfClass(DirectShape).WhereElementIsNotElementType())
for shape_index, el in enumerate(shapes):
    add_element_row(el, 'DIRECTSHAPE', shape_index < SAMPLE_DIRECTSHAPES,
                    'All DirectShapes inventoried; geometry only sampled')
for group in FilteredElementCollector(doc).OfClass(Group).WhereElementIsNotElementType():
    entry = add_element_row(group, 'GROUP', False)
    try: entry['Note'] = 'MemberCount='+txt(len(list(group.GetMemberIds())))
    except Exception: pass
for assembly in FilteredElementCollector(doc).OfClass(AssemblyInstance).WhereElementIsNotElementType():
    entry = add_element_row(assembly, 'ASSEMBLY', False)
    try: entry['Note'] = 'MemberCount='+txt(len(list(assembly.GetMemberIds())))
    except Exception: pass
for imp in FilteredElementCollector(doc).OfClass(ImportInstance).WhereElementIsNotElementType():
    add_element_row(imp, 'PROJECT_CAD', False)
for link in FilteredElementCollector(doc).OfClass(RevitLinkInstance).WhereElementIsNotElementType():
    add_element_row(link, 'REVIT_LINK', False, 'Linked model content not inspected by this script')

# V5 link audit: explicitly count instances AND link types. V4 could report zero
# even when linked types exist; record the distinction and exceptions.
link_audit_rows = []
try:
    link_instances_v5 = list(FilteredElementCollector(doc).OfClass(RevitLinkInstance))
    link_types_v5 = list(FilteredElementCollector(doc).OfClass(RevitLinkType))
    for link in link_instances_v5:
        try:
            tp = doc.GetElement(link.GetTypeId())
            link_audit_rows.append({'Scope':'INSTANCE','Element ID':idstr(link),
                'Name':element_name(link), 'Type ID':idstr(tp) if tp else '',
                'Type Name':element_name(tp) if tp else '', 'Status':'PRESENT', 'Error':''})
        except Exception as ex:
            link_audit_rows.append({'Scope':'INSTANCE','Element ID':idstr(link),
                'Name':'','Type ID':'','Type Name':'','Status':'API_EXCEPTION','Error':errmsg(ex)})
    for lt in link_types_v5:
        status = 'UNKNOWN'
        try:
            status = 'LOADED' if RevitLinkType.IsLoaded(doc, lt.Id) else 'UNLOADED'
        except Exception as ex:
            status = 'LOAD_STATUS_UNAVAILABLE: ' + errmsg(ex)
        link_audit_rows.append({'Scope':'TYPE','Element ID':idstr(lt),
            'Name':element_name(lt),'Type ID':idstr(lt),'Type Name':element_name(lt),
            'Status':status,'Error':''})
except Exception as ex:
    link_audit_rows.append({'Scope':'COLLECTION','Element ID':'','Name':'',
        'Type ID':'','Type Name':'','Status':'API_EXCEPTION','Error':errmsg(ex)})

# Export actionable identity for geometry exceptions in sample of component families.
for fr in family_rows:
    if fr['Geometry Probe'] != 'EXCEPTION': continue
    fid = fr['Family Element ID']
    for inst in family_instances.get(fid, [])[:GEOMETRY_SAMPLES_PER_FAMILY]:
        add_element_row(inst, 'FAMILY_EXCEPTION_FOLLOWUP', True, fr['Family Name'])

# ---- V5: Autodesk fabrication content audit ----
# Fabrication parts are NOT FamilyInstances. Their geometry needs direct testing.
# Do not interpret null geometry or empty parameters as proof of translator failure.
fab_rows, fab_issue_rows, fab_group_rows = [], [], []
fab_all, fab_selected = [], []
fab_api_error = ''
fab_collection_method = ''
fab_expected_count = 0
# Category collection works even when the nested Fabrication namespace cannot be imported.
try:
    fab_category = BuiltInCategory.OST_FabricationPipework
    fab_all = list(FilteredElementCollector(doc).OfCategory(fab_category).WhereElementIsNotElementType())
    fab_collection_method = 'OST_FabricationPipework category'
except Exception as ex:
    fab_api_error = 'Category collection failed: ' + errmsg(ex)

# Only use the .NET class collector as a fallback; otherwise don't require its import.
if not fab_all and FabricationPart is not None:
    try:
        fab_all = list(FilteredElementCollector(doc).OfClass(FabricationPart).WhereElementIsNotElementType())
        fab_collection_method = 'FabricationPart .NET class'
    except Exception as ex:
        fab_api_error += ' | Class collection failed: ' + errmsg(ex)

# Independent check against V4 category inventory: zero matches cannot be success.
try:
    fab_expected_count = sum(int(r['Element Count']) for r in category_rows
                             if r['Category'] == 'MEP Fabrication Pipework')
except Exception:
    fab_expected_count = 0
fab_scan_warnings = []
if fab_expected_count and not fab_all:
    fab_scan_warnings.append('CRITICAL: Inventory has %s fabrication-category elements but scanner collected 0.' % fab_expected_count)
elif fab_expected_count and len(fab_all) != fab_expected_count:
    fab_scan_warnings.append('WARNING: Category inventory count %s differs from fabrication collector %s.' % (fab_expected_count, len(fab_all)))

def fattr(obj, attr):
    try:
        x = getattr(obj, attr)
        if x is None: return ''
        return txt(x)
    except Exception: return ''

def float_or_blank(value):
    try:
        v = float(value)
        return ('%.5f' % v) if not math.isnan(v) and not math.isinf(v) else ''
    except Exception: return ''

def fabrication_metadata(el):
    # These members are version-/configuration-dependent, so a missing value
    # is simply unavailable (not a warning or diagnosed defect).
    d = {'Service ID':fattr(el,'ServiceId'), 'CID':fattr(el,'ItemCustomId'),
         'Item Number':fattr(el,'ItemNumber'), 'Product Name':fattr(el,'ProductName'),
         'Part Type':fattr(el,'PartType'), 'Service Name':'',
         'Item Description':fattr(el,'ItemDescription'),
         'Material':'', 'Fabrication Dimensions':'', 'Notes':''}
    try:
        sid = el.ServiceId
        svc = doc.GetElement(sid) if hasattr(sid, 'IntegerValue') or hasattr(sid, 'Value') else None
        if svc: d['Service Name'] = element_name(svc)
    except Exception: pass
    try:
        d['Material'] = fattr(el,'MaterialName')
    except Exception: pass
    try:
        dimensions = []
        # API GetDimensions() may return FabricationDimensionDefinition objects.
        for dim in el.GetDimensions():
            try:
                dim_name = fattr(dim,'Name') or fattr(dim,'DisplayName')
                val = el.GetDimensionValue(dim)
                dimensions.append('%s=%s' % (dim_name, float_or_blank(val)))
            except Exception:
                continue
        d['Fabrication Dimensions'] = '; '.join(dimensions)[:1200]
    except Exception: pass
    # Project-dependent parameter names; retain blanks when no value is available.
    if not d['Service Name']:
        d['Service Name']=fabrication_parameter_fallback(el,['Fabrication Service','Service Name','Service'])
    if not d['Item Description']:
        d['Item Description']=fabrication_parameter_fallback(el,['Item Description','Description','Fabrication Description'])
    if not d['Material']:
        d['Material']=fabrication_parameter_fallback(el,['Material','Fabrication Material','Material Name'])
    if not d['Item Number']:
        d['Item Number']=fabrication_parameter_fallback(el,['Item Number','Item No.'])
    return d

# Group by actual available service/CID metadata, then round-robin across groups
# for representative sample coverage without random data-dependent run-to-run changes.
fab_groups = defaultdict(list)
for p in fab_all:
    md = fabrication_metadata(p)
    group_key = (md['Service ID'] or '<unknown>', md['CID'] or '<unknown>')
    fab_groups[group_key].append((p, md))
keys = sorted(fab_groups.keys())
for k in keys:
    fab_groups[k].sort(key=lambda pair: idstr(pair[0]))
limit = len(fab_all) if FABRICATION_SCAN_LIMIT < 0 else min(FABRICATION_SCAN_LIMIT, len(fab_all))
selected_ids = set()
# Put at least one from each group when budget allows; then round-robin.
pos = 0
while len(fab_selected) < limit:
    progressed = False
    for k in keys:
        group = fab_groups[k]
        if pos < len(group):
            pair = group[pos]
            fid = idstr(pair[0])
            if fid not in selected_ids:
                fab_selected.append(pair)
                selected_ids.add(fid)
                progressed = True
                if len(fab_selected) >= limit: break
    if not progressed: break
    pos += 1

fab_outcomes = defaultdict(lambda: defaultdict(int))
for index, pair in enumerate(fab_selected):
    el, md = pair
    state, details, stats = deep_geometry_probe(el)
    gid = (md['Service ID'] or '<unknown>', md['CID'] or '<unknown>')
    fab_outcomes[gid][state] += 1
    row = {'Element ID':idstr(el),'Unique ID':safe_uid(el),'Category':catname(el),
        'Family/Type':typename(el),'Workset':worksetinfo(el),'Phase':phaseinfo(el),
        'Bounding Box (feet)':bboxinfo(el),'Geometry Probe':state,
        'Geometry Detail':details,'Sample Sequence':index+1,
        'Solid Count':stats['solids'],'Face Count':stats['faces'],
        'Edge Count':stats['edges'],'Mesh Count':stats['meshes'],
        'Triangle Count':stats['triangles'],'Geometry Instance Count':stats['instances'],
        'Volume (ft3)':float_or_blank(stats.get('volume_ft3',0)),
        'Geometry Max Depth':stats['max_depth'],'Geometry Truncations':stats['truncated']}
    row.update(md)
    fab_rows.append(row)
    if state in ('EXCEPTION','NESTED_GEOMETRY_EXCEPTION','TRUNCATED'):
        fab_issue_rows.append({'Priority':'HIGH','Element ID':idstr(el),'Unique ID':safe_uid(el),
            'Service ID':md['Service ID'],'CID':md['CID'],
            'Evidence':('API_GEOMETRY_EXCEPTION' if state != 'TRUNCATED' else 'GEOMETRY_TRUNCATED'),'Details':details,
            'Next Action':'Locate by element ID; inspect in a separate troubleshooting copy'})
        issue_rows.append(issue_record('HIGH','FABRICATION GEOMETRY',md['Product Name'],
            md['CID'],idstr(el),details,'API_EXCEPTION','Inspect element; supply ID to Autodesk'))
    if (index+1) % FABRICATION_BATCH_SIZE == 0:
        print('V5.2 fabrication geometry tested %s / %s' % (index+1,len(fab_selected)))

for k in keys:
    counter = fab_outcomes[k]
    fab_group_rows.append({'Service ID':k[0],'CID':k[1],
        'Total Parts':len(fab_groups[k]),'Tested Parts':sum(counter.values()),
        'Geometry OK':counter.get('OK',0),'Geometry Exceptions':counter.get('EXCEPTION',0)+counter.get('NESTED_GEOMETRY_EXCEPTION',0),
        'Geometry Truncated':counter.get('TRUNCATED',0),
        'No Geometry Returned':counter.get('NO_GEOMETRY_RETURNED',0),
        'Untested Parts':len(fab_groups[k])-sum(counter.values()),
        'Interpretation':'Sample only - not a cloud translation verification'})
fab_group_rows.sort(key=lambda r:(-r['Geometry Exceptions'],-r['Total Parts'],r['Service ID'],r['CID']))

# Revit-hosted samples and linked/model subsystems use different scope categories.
# These are suggested *manual* test groups, not a claim of causality.
isolation_rows.extend([
    {'Test Order':1, 'Isolation Group':'REVIT LINKS', 'Element Count':len(list(FilteredElementCollector(doc).OfClass(RevitLinkInstance))),
     'Action In Disposable Copy':'Unload all Revit links; retry an isolated publish',
     'Interpretation':'Success suggests a linked-model dependency; then isolate links in halves',
     'Precaution':'Unloading links may affect host relationships and coordination; never change production file'},
    {'Test Order':2, 'Isolation Group':'DIRECTSHAPES', 'Element Count':len(shapes),
     'Action In Disposable Copy':'Temporarily isolate/remove DirectShapes in a TEST copy only, then retry translation',
     'Interpretation':'Success suggests one or more DirectShapes; narrow by halves',
     'Precaution':'Record original element IDs before changes and never replace the production model'},
    {'Test Order':3, 'Isolation Group':'PROJECT CAD IMPORTS', 'Element Count':len(list(FilteredElementCollector(doc).OfClass(ImportInstance))),
     'Action In Disposable Copy':'Remove/import-isolate project CAD in a TEST copy only',
     'Interpretation':'Success narrows investigation to CAD or dependencies',
     'Precaution':'Preserve native project, investigate any dependent components'},
    {'Test Order':4, 'Isolation Group':'SYSTEM ELEMENT CATEGORIES', 'Element Count':sum(len(category_buckets[r['Category']]) for r in category_rows if r['Sampled Count'] > 0),
     'Action In Disposable Copy':'Test subsets of system elements grouped by category, if earlier tests fail',
     'Interpretation':'A successful translation is evidence about the tested subset only',
     'Precaution':'A valid cloud publish test is required; a local geometry probe cannot reproduce cloud translation'}
])

isolation_rows.append({'Test Order':5,'Isolation Group':'FABRICATION PARTS',
    'Element Count':len(fab_all),
    'Action In Disposable Copy':'Use Fabrication_Groups.csv to create reproducible halves; translate isolated test copies only',
    'Interpretation':'Geometry exceptions rank first; an OK local probe does not clear cloud translation',
    'Precaution':'Never delete in production, and never publish test copies into production Stratus project tracking'})

# Build prioritized shortlist, distinguishing API exceptions from soft naming flags.
ranked = []
for r in family_rows:
    fid = r['Family Element ID']
    related_type_issues = [x for x in type_rows if x['Family Element ID']==fid and x['Priority']!='OK']
    api_ex = r['Geometry Probe']=='EXCEPTION' or r['Family Open Test']=='EXCEPTION'
    cad_count = r['Family Internal CAD Imports']
    has_cad = isinstance(cad_count,int) and cad_count>0
    hidden = ('HIDDEN_OR_CONTROL_UNICODE' in r['Name Flags'] or
              any('HIDDEN_OR_CONTROL_UNICODE' in x['Name Flags'] for x in related_type_issues))
    invalid = ('REVIT_INVALID_NAME' in r['Name Flags'] or
               any('REVIT_INVALID_NAME' in x['Name Flags'] for x in related_type_issues))
    unicode_review = ('NON_ASCII_REVIEW' in r['Name Flags'] or
               any('NON_ASCII_REVIEW' in x['Name Flags'] for x in related_type_issues))
    collision = 'NORMALIZED_NAME_COLLISION' in r['Name Flags']
    reason=[]
    if api_ex: reason.append('API_EXCEPTION')
    if hidden: reason.append('HIDDEN_UNICODE')
    if invalid: reason.append('REVIT_INVALID_NAME')
    if has_cad: reason.append('EMBEDDED_CAD')
    if collision: reason.append('NAME_COLLISION')
    if unicode_review: reason.append('NON_ASCII')
    if not reason: continue
    score = (100 if api_ex else 0)+(70 if hidden else 0)+(65 if invalid else 0)+(30 if has_cad else 0)+(15 if collision else 0)+(5 if unicode_review else 0)
    action = ('Investigate exception in family editor' if api_ex else
              'Inspect embedded CAD in family editor' if has_cad else
              'Review exact Unicode and type name; do not rename automatically')
    ranked.append({'Score':score,'Reason':'; '.join(reason),'Family Name':r['Family Name'],
      'Category':r['Category'],'Family Element ID':fid,'Placed Instances':r['Placed Instances'],
      'Geometry Probe':r['Geometry Probe'],'Family Open Test':r['Family Open Test'],
      'Family Internal CAD Imports':cad_count,'Recommended Next Step':action})
ranked.sort(key=lambda x:(-x['Score'],-x['Placed Instances'],x['Family Name']))

files = [
 ('Fabrication_Parts', ['Sample Sequence','Element ID','Unique ID','Category','Family/Type','Service ID',
    'Service Name','CID','Item Number','Product Name','Part Type','Item Description','Material',
    'Fabrication Dimensions','Workset','Phase','Bounding Box (feet)','Geometry Probe',
    'Geometry Detail','Solid Count','Face Count','Edge Count','Mesh Count','Triangle Count',
    'Geometry Instance Count','Volume (ft3)','Geometry Max Depth','Geometry Truncations','Notes'], fab_rows),
 ('Fabrication_Groups', ['Service ID','CID','Total Parts','Tested Parts','Geometry OK',
    'Geometry Exceptions','Geometry Truncated','No Geometry Returned','Untested Parts','Interpretation'],fab_group_rows),
 ('Fabrication_Issues', ['Priority','Element ID','Unique ID','Service ID','CID','Evidence',
    'Details','Next Action'],fab_issue_rows),
 ('Link_Audit', ['Scope','Element ID','Name','Type ID','Type Name','Status','Error'],link_audit_rows),
 ('Element_Samples', ['Scope','Category','Element Class','Element ID','Unique ID','Name','Type Name',
          'Workset','Phase','Owner View','Bounding Box (feet)','Geometry Probe','Geometry Detail','Note'],element_rows),
 ('Category_Inventory', ['Category','Element Count','Element Classes','Sampled Count','Exceptions','Notes'], category_rows),
 ('Isolation_Plan', ['Test Order','Isolation Group','Element Count','Action In Disposable Copy',
          'Interpretation','Precaution'],isolation_rows),
 ('Shortlist', ['Score','Reason','Family Name','Category','Family Element ID','Placed Instances',
                 'Geometry Probe','Family Open Test','Family Internal CAD Imports','Recommended Next Step'], ranked),
 ('Families', ['Priority','Family Name','Category','Family Element ID','Family Unique ID','Types','Placed Instances',
               'Editable','In Place','Name Flags','Unicode Characters','Nested Shared Subfamilies (placed)',
               'Geometry Probe','Geometry Detail','Family Open Test','Family Internal CAD Imports',
               'Nested Families (family editor)','Deep Scan Details'],family_rows),
 ('Types', ['Priority','Family Name','Type Name','Category','Family Element ID','Type Element ID','Type Unique ID',
            'Placed Instances','Name Flags','Unicode Characters'],type_rows),
 ('Issues', ['Severity','Scope','Family','Type','Element ID','Details','Evidence','Recommended Action'],issue_rows),
 ('CAD_and_Links', ['Location','Family Name','Element ID','Name','Kind'],import_rows),
]
paths=[]
for suffix, fields, rows in files:
    p = os.path.join(folder, base + '_' + suffix + '.csv')
    export_csv(p,fields,rows)
    paths.append(p)

# Human-readable summary; triage only, not a cloud translation test.
def ex_html(value):
    return (txt(value).replace('&','&amp;').replace('<','&lt;')
            .replace('>','&gt;').replace('"','&quot;').replace("'",'&#39;'))

def ex_save(path, value):
    if sys.version_info[0] < 3:
        with open(path,'wb') as f: f.write(value.encode('utf-8'))
    else:
        with open(path,'w',encoding='utf-8') as f: f.write(value)

def generate_executive_reports():
    total = len(fab_all)
    tested = len(fab_rows)
    requested = total if FABRICATION_SCAN_LIMIT < 0 else min(FABRICATION_SCAN_LIMIT,total)
    valid = tested == requested and (not fab_expected_count or total == fab_expected_count)
    if fab_expected_count and not total: valid = False
    bad = [r for r in fab_rows if r.get('Geometry Probe') in ('EXCEPTION','NESTED_GEOMETRY_EXCEPTION','TRUNCATED')]
    high = [r for r in issue_rows if txt(r.get('Severity','')).upper() in ('HIGH','CRITICAL')]
    links = [r for r in link_audit_rows if r.get('Scope') == 'INSTANCE']
    coord = sum(int(r.get('Element Count',0)) for r in category_rows if 'coordination model' in txt(r.get('Category','')).lower())
    coverage = (tested*100.0/total) if total else 0
    if not valid:
        status, explanation = 'INCOMPLETE SCAN', 'Sample coverage or fabrication inventory did not reconcile; first fix the scan.'
    elif bad or high:
        status, explanation = 'ACTION REQUIRED', 'Local geometry errors or high-priority scan findings need inspection.'
    else:
        status, explanation = 'NO LOCAL API ERRORS DETECTED', 'This does NOT verify Autodesk translation; untested content may remain.'
    actions=[]
    if not valid:
        actions.append(('HIGH','Fix incomplete scan','Review the timestamped README and rescan before interpreting the results.'))
    for rec in fab_issue_rows[:5]:
        actions.append(('HIGH','Inspect fabrication Element ID '+txt(rec.get('Element ID','')),
                        txt(rec.get('Evidence',''))+': '+txt(rec.get('Details',''))[:180]))
    for rec in high[:5]:
        if rec.get('Scope') == 'FABRICATION GEOMETRY': continue
        actions.append(('HIGH','Inspect element '+txt(rec.get('Element ID','')),
                        txt(rec.get('Scope',''))+': '+txt(rec.get('Details',''))[:180]))
    if coord:
        actions.append(('REVIEW','Check coordination models (%s)' % coord,
                        'Confirm inclusion in published view; isolate NWC coordination references in a disposable test copy.'))
    if links:
        actions.append(('REVIEW','Check Revit links (%s instances)' % len(links),
                        'If earlier tests fail, unload links in a disposable copy and rerun cloud translation.'))
    if ranked:
        r=ranked[0]
        actions.append(('REVIEW','Inspect family '+txt(r['Family Name']),
                        'Family ID '+txt(r['Family Element ID'])+'; reason: '+txt(r['Reason'])))
    if not actions:
        actions.append(('NEXT','Run a controlled cloud translation test',
                        'No definitive culprit found locally. Compare the exact publish view and test model content in isolation.'))
    if total > tested:
        actions.append(('COVERAGE','Consider a wider geometry scan',
                        '%s/%s fabrication parts tested (%.2f%%). Local sampling is not cloud validation.' % (tested,total,coverage)))
    actions=actions[:8]
    table=''.join('<tr><td>%s</td><td><b>%s</b></td><td><b>%s</b><p>%s</p></td></tr>' %
                  (i+1,ex_html(a),ex_html(b),ex_html(c)) for i,(a,b,c) in enumerate(actions))
    style='''<style>@page{size:letter;margin:.6in}body{font:13px/1.45 Segoe UI,Arial,sans-serif;color:#203043;max-width:980px;margin:30px auto;padding:0 15px}h1{font-size:25px;margin-bottom:2px}h2{font-size:16px;margin-top:24px}.muted{color:#526377}.status{background:#f0f3f6;border-left:5px solid #8b6914;padding:14px;margin:18px 0}.stats{display:flex;flex-wrap:wrap;gap:12px}.stat{border:1px solid #d5dfe7;border-radius:5px;padding:10px;min-width:120px}.stat b{font-size:20px;display:block}table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid #d5dfe7;padding:8px;text-align:left;vertical-align:top}th{background:#e9f0f5}td:nth-child(1){width:30px}td:nth-child(2){width:85px}td p{margin:3px 0}.foot{border-top:1px solid #d5dfe7;padding-top:12px;margin-top:16px}@media print{body{margin:0;padding:0}tr{break-inside:avoid}}</style>'''
    html='''<!doctype html><html><head><meta charset="utf-8"><title>Stratus Executive Diagnostic Summary</title>%s</head><body><p class="muted">STRATUS / REVIT MODEL DIAGNOSTIC</p><h1>Executive Scan Summary</h1><p><b>Model:</b> %s<br><b>Run:</b> %s</p><div class="status"><b>%s</b><p>%s</p><b>Local diagnostics cannot confirm an Autodesk cloud translation PASS.</b></div><div class="stats"><div class="stat"><b>%s</b>Fabrication parts</div><div class="stat"><b>%s</b>Parts tested</div><div class="stat"><b>%s</b>Fabrication geometry alerts</div><div class="stat"><b>%.2f%%</b>Coverage</div></div><h2>What to look at first</h2><table><thead><tr><th>#</th><th>Priority</th><th>Finding / next action</th></tr></thead><tbody>%s</tbody></table><h2>Scan context</h2><p>Families: %s | Revit link instances: %s | Coordination-model category elements: %s | Deep family scan: %s.</p><p>Scan completion: %s. %s fabrication parts remain untested.</p><div class="foot"><b>How to use this report:</b> For any listed ID, use Revit's Select by ID. Test removals/unloads only in disposable copies using isolated Stratus destinations. Save the detailed CSV reports for Autodesk support. No model content was modified or uploaded by this diagnostic.</div></body></html>''' % (
        style,ex_html(model),ex_html(started),ex_html(status),ex_html(explanation),
        total,tested,len(bad),coverage,table,len(families),len(links),coord,
        ex_html(DEEP_FAMILY_SCAN),ex_html('COMPLETE' if valid else 'INCOMPLETE'),max(0,total-tested))
    text_lines=['STRATUS EXECUTIVE DIAGNOSTIC SUMMARY','Model: '+txt(model),'Run: '+txt(started),
        'STATUS: '+status,explanation,'NOT A CLOUD TRANSLATION PASS.','',
        'Fabrication: %s parts | %s tested | %.2f%% coverage | %s geometry issues' % (total,tested,coverage,len(bad)),
        'Revit links: %s | Coordination models: %s | Families: %s' % (len(links),coord,len(families)),
        '','WHAT TO LOOK AT FIRST:']
    for i,(priority,title,detail) in enumerate(actions):
        text_lines.append('%s. [%s] %s - %s' % (i+1,priority,title,detail))
    text_lines.extend(['','Review detailed CSVs for all element IDs.','Only use disposable copies for destructive testing.'])
    html_path=os.path.join(folder,base+'_EXECUTIVE_SUMMARY.html')
    text_path=os.path.join(folder,base+'_EXECUTIVE_SUMMARY.txt')
    ex_save(html_path,html)
    ex_save(text_path,u'\n'.join(text_lines)+u'\n')
    return [html_path,text_path]

summary = os.path.join(folder,base+'_README.txt')
with open(summary,'w') as ff:
    ff.write('Stratus Revit translation diagnostic V5.3\n')
    ff.write('Model: '+model+'\nRun: '+txt(started)+'\n')
    ff.write('Families: %s | Types: %s | Instances: %s | Findings: %s | Links/CAD: %s\n' %
             (len(families),len(type_rows),len(instances),len(issue_rows),len(import_rows)))
    ff.write('Deep family scan: %s | Geometry sampling: %s examples per family\n' %
             (DEEP_FAMILY_SCAN, GEOMETRY_SAMPLES_PER_FAMILY if CHECK_INSTANCE_GEOMETRY else 0))
    ff.write('IMPORTANT: Flags are investigation leads, not proof of cloud translation failure.\n')
    ff.write('Ranked shortlist: %s families. Common plumbing punctuation does not create findings.\n' % len(ranked))
    ff.write('Deep Scan opens families without saving; no edits or Stratus publish performed.\n')
    ff.write('Project element count: %s | categories: %s | DirectShapes: %s | reportable elements: %s\n' % (len(all_instances), len(category_rows),len(shapes),len(element_rows)))
    ff.write('Nested-family counts now represent placed FamilyInstances in family docs, NOT loaded definitions.\n')
    ff.write('V5.3 fabrication: %s total | %s geometry tested | %s exceptions | %s metadata groups\n' %
             (len(fab_all),len(fab_rows),len(fab_issue_rows),len(fab_group_rows)))
    ff.write('Fabrication collection: %s | inventory expected: %s | collected: %s\n' %
             (fab_collection_method or 'UNAVAILABLE', fab_expected_count, len(fab_all)))
    ff.write('Fabrication API availability: %s\n' % (fab_api_error or 'No blocking collection error'))
    ff.write('Fabrication import available: %s\n' % (FabricationPart is not None))
    expected_samples = len(fab_all) if FABRICATION_SCAN_LIMIT < 0 else min(FABRICATION_SCAN_LIMIT,len(fab_all))
    if fab_expected_count and not fab_all:
        scan_status = 'INCOMPLETE - FABRICATION COLLECTION FAILED'
    elif len(fab_rows) != expected_samples:
        scan_status = 'INCOMPLETE - REQUESTED SAMPLES NOT COMPLETED'
    else:
        scan_status = 'COMPLETE - LOCAL API SAMPLING ONLY'
    ff.write('Fabrication scan validation: %s | requested: %s | completed: %s\n' %
             (scan_status, expected_samples, len(fab_rows)))
    for warning in fab_scan_warnings:
        ff.write(warning + '\n')
    ff.write('Revit link audit: %s instances | %s types (V4 count may have been inaccurate)\n' %
             (len([r for r in link_audit_rows if r['Scope']=='INSTANCE']),
              len([r for r in link_audit_rows if r['Scope']=='TYPE'])))
    ff.write('V5.3 recursive results: %s nested exceptions | %s truncated | %s geometry-null | %s positive-volume solids\n' % (
        sum(1 for r in fab_rows if r['Geometry Probe']=='NESTED_GEOMETRY_EXCEPTION'),
        sum(1 for r in fab_rows if r['Geometry Probe']=='TRUNCATED'),
        sum(1 for r in fab_rows if r['Geometry Probe']=='NO_GEOMETRY_RETURNED'),
        sum(1 for r in fab_rows if int(r['Solid Count'])>0)))
    ff.write('Recursive traversal caps: depth=%s objects/element=%s. Flags are investigation leads, not translator confirmation.\n' % (DEEP_GEOMETRY_MAX_DEPTH,DEEP_GEOMETRY_MAX_OBJECTS))
    ff.write('Coverage: fabrication sampling is deterministic by service/CID; remaining parts are untested.\n')
    ff.write('V5.3 outputs Fabrication_Parts, Fabrication_Groups, Fabrication_Issues and Link_Audit.\n')
    ff.write('Geometry probes sample placed instances only; unplaced types, buried imports and unshared nested families\n')
    ff.write('may require deep scan/manual inspection. Zero or null geometry is not inherently a defect.\n')
    ff.write('No data was uploaded; files are on your machine only.\n')
paths.append(summary)
try:
    paths.extend(generate_executive_reports())
except Exception as ex:
    print('Executive summary generation failed: ' + errmsg(ex))

message = ('Stratus diagnostic V5.3 complete: %s families, %s types, %s instances; %s findings.\n\n%s' %
           (len(families),len(type_rows),len(instances),len(issue_rows),'\n'.join(paths)))
if fab_scan_warnings:
    message += '\n\nSCAN WARNING: ' + '; '.join(fab_scan_warnings)
if output:
    output.print_md('### Stratus / Autodesk Translator Family Diagnostic V5.3')
    output.print_md('**%s**' % txt(message).replace('\n','  \n'))
else:
    from Autodesk.Revit.UI import TaskDialog
    TaskDialog.Show('Stratus Family Diagnostic',message[:1800])
