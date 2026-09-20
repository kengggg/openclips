#!/usr/bin/env python3
"""Generate links.proto (BLE control protocol) from extracted javalite schemas.

Reads schemas.json (from schema_dex_extract.py over the single-out tree),
maps obfuscated class names to semantic names using:
  - his.java enum (request type numbers)
  - btq.java request->response mapping
  - hin.java protocol version
Then emits a single links.proto with Request/Response and all payloads.
"""
import json, re, sys, os

# tc < 51: FieldType ordinals (javalite old)
PRIM = {0:'double',1:'float',2:'int64',3:'uint64',4:'int32',5:'fixed64',6:'fixed32',
        7:'bool',8:'string',9:'group',10:'message',11:'bytes',12:'uint32',13:'enum',
        14:'sfixed32',15:'sfixed64',16:'sint32',17:'sint64'}
REP = {18:'double',19:'float',20:'int64',21:'uint64',22:'int32',23:'fixed64',24:'fixed32',
       25:'bool',26:'string',27:'message',28:'bytes',29:'uint32',30:'enum',31:'sfixed32',
       32:'sfixed64',33:'sint32',34:'sint64'}

def type_for(field, class_ref):
    tc = field['tc']
    if tc >= 51:
        base = tc - 51
        # base 9/17 consume class ref (message/enum)
        return ('message', class_ref)
    if tc in PRIM:
        return ('scalar', PRIM[tc])
    if tc in REP:
        return ('repeated_' , REP[tc])
    return ('unknown_tc%d' % tc, None)

def camel(name):
    return ''.join(p.capitalize() for p in re.split(r'[_.]', name))

def main(schemas_path, out_path):
    data = json.load(open(schemas_path))
    # class-name -> semantic name map will be filled progressively
    # Start with known: hir=Request, hiw=Response, hin=ProtocolVersion, his=RequestType(enum)
    # payload mapping from hir objarr order vs field numbers:
    known = {'hir':'Request', 'hiw':'Response', 'hin':'ProtocolVersion'}
    lines = []
    lines.append('// Google Clips BLE control protocol — links.proto')
    lines.append('// Reconstructed from com.google.android.apps.cerebra.links 1.8.245834203')
    lines.append('// via javalite schema decoding. Field names are semantic guesses where noted.')
    lines.append('syntax = "proto2";')
    lines.append('')
    lines.append('enum RequestType {')
    # from his.java
    req_enum = [(1,'PUBLIC_QUERY'),(2,'PRIVATE_QUERY'),(3,'KEEP_ALIVE'),(4,'INITIATE_PAIRING'),
        (5,'CANCEL_PAIRING'),(6,'INITIATE_SECURE_CONNECTION'),(7,'COMPLETE_SECURE_CONNECTION'),
        (8,'SET_UPDATE_REQUIRED'),(9,'INITIATE_WIFI'),(10,'CANCEL_WIFI'),(11,'LIST_SESSIONS'),
        (12,'LIST_MOMENTS'),(13,'GET_PLACEHOLDER_IMAGE'),(14,'DELETE_MOMENTS'),(15,'SET_FLAG_EXPERIMENT_OVERRIDES'),
        (16,'GET_FLAG_FINGERPRINTS'),(17,'GET_FLAGS'),(18,'SET_FLAG_MANUAL_OVERRIDES'),(19,'SHUTDOWN'),
        (20,'DELETE_SESSION'),(21,'COMPLETE_CURRENT_SESSION'),(22,'SET_SESSION_MODE'),(27,'INITIATE_CAPTURE_PREVIEW'),
        (28,'CANCEL_CAPTURE_PREVIEW'),(29,'SET_COVER_STATE'),(30,'SET_CLOUD_UPLOAD_WIFI_NETWORK'),
        (31,'INITIATE_CLOUD_UPLOAD'),(32,'CANCEL_CLOUD_UPLOAD'),(33,'FETCH_LARP_SESSION_INDEX'),
        (34,'FETCH_LARP_SESSION_METADATA'),(35,'FLASH_IDENTIFY_LEDS'),(36,'GET_SYSTEM_LOGS'),
        (37,'TRIGGER_CAPTURE'),(39,'ECHO'),(40,'ACK_NEW_CONTENT'),(41,'GET_PREFERENCES'),
        (42,'SET_PREFERENCES'),(43,'COMPLIANCE_POLICY_UPDATE'),(44,'APPLY_FAMILIAR_PEOPLE_IMPORT'),
        (45,'RESET_FAMILIAR_PEOPLE_LIST'),(46,'SET_PAIRING_SETTINGS'),(47,'INITIATE_ADDITIONAL_PAIRING'),
        (48,'COMPLETE_ADDITIONAL_PAIRING'),(49,'MOVE_MOMENTS_TO_TRASH'),(50,'RESTORE_MOMENTS_FROM_TRASH'),
        (51,'ACTIVE_USER_SETTINGS'),(52,'MULTIPLE_PAIRING_STATUS'),(53,'FETCH_FAMILIAR_PEOPLE_LIST'),
        (54,'GET_PAIRING_PASSWORD'),(55,'FORGET_FAMILIAR_PEOPLE'),(56,'MERGE_FAMILIAR_PEOPLE'),
        (57,'FETCH_PERSON_ID_MAP'),(2048,'DISABLE_CONNECTION_TIMEOUTS')]
    for num, name in req_enum:
        lines.append(f'  {name} = {num};')
    lines.append('}')
    lines.append('')

    # Write every extracted schema as a message
    for cls, entry in sorted(data.items()):
        short = cls.split('.')[-1]
        sem = known.get(short, short)
        schema = entry['schema']
        fields = schema.get('fields', [])
        if not fields:
            continue
        objarr = [e[1] for e in entry['objarr']]
        lines.append(f'message {sem} {{  // {short}')
        # class refs consumed in order for tc in {27,49,12cond,30,44,50} & tc>=51 base 9/17
        cls_i = 0
        name_i = 0
        for f in fields:
            tc = f['tc']
            num = f['num']
            label = 'optional'
            ftype = 'int32'
            jname = None
            if tc >= 51:
                base = tc - 51
                # base 9/17 = message w/ class ref
                if base in (9, 17):
                    if cls_i < len(objarr):
                        ref = objarr[cls_i]; cls_i += 1
                    else:
                        ref = 'Msg%d' % num
                    ftype = ref if ref != 'Msg%d' % num else ref
                    jname = None
                else:
                    ftype = f'MSGTYPE_{tc}_{num}'
            elif tc in PRIM:
                ftype = PRIM[tc]
                # presence/name from objarr order
            elif tc in REP:
                label = 'repeated'
                ftype = REP[tc]
            # java field name for scalars comes from objarr in order
            if tc < 51 and name_i < len(objarr) and objarr and isinstance(objarr[name_i], str) and len(objarr[name_i]) <= 3:
                jname = objarr[name_i]; name_i += 1
            fname = jname or ('field_%d' % num)
            lines.append(f'  {label} {ftype} {fname} = {num};  // tc={tc} packed={f["packed"]:#x}')
        lines.append('}')
        lines.append('')
    open(out_path, 'w').write('\n'.join(lines))
    print(f"wrote {out_path}: {len(data)} messages")

if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])