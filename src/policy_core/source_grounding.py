"""Literal number/unit anchors and dictionary-defined source terms."""
import re

DIGITS=dict(zip('零〇一二两三四五六七八九壹贰叁肆伍陆柒捌玖', [0,0,1,2,2,3,4,5,6,7,8,9,1,2,3,4,5,6,7,8,9]))
UNITS={'十':10,'拾':10,'百':100,'佰':100,'千':1000,'仟':1000,'万':10000,'亿':100000000}

def chinese_number(text):
    total=section=number=0
    if not any(ch in UNITS for ch in text):return int(''.join(str(DIGITS[ch]) for ch in text))
    for ch in text:
        if ch in DIGITS:number=DIGITS[ch]
        elif UNITS[ch]<10000:section+=(number or 1)*UNITS[ch];number=0
        elif UNITS[ch]==10000:section=(section+number)*10000;number=0
        else:total=(total+section+number)*100000000;section=number=0
    return total+section+number

def numbers(text):
    out=set()
    for value,unit in re.findall(r'(\d+(?:\.\d+)?)\s*([万千百]?)',text):out.add(float(value)*{'万':10000,'千':1000,'百':100}.get(unit,1))
    for match in re.finditer(r'[零〇一二两三四五六七八九十百千万亿壹贰叁肆伍陆柒捌玖拾佰仟]+',text):
        preceding=text[:match.start()].rstrip()
        if preceding and preceding[-1].isdigit():continue
        out.add(chinese_number(match.group()))
    return out

def validate(atom,definition):
    text='\n'.join(cite['quote'] for cite in atom.get('evidence',[]))
    if definition['type']=='number' and atom['op']!='exists':
        expected=atom.get('value');expected=expected if isinstance(expected,list) else [expected]
        anchored=numbers(text)
        if any(value not in anchored for value in expected):raise ValueError('unsupported_numeric_policy_constraint:'+atom['field'])
    for synonyms in definition.get('source_terms_all',[]):
        if not any(term in text for term in synonyms):raise ValueError('unsupported_policy_source_terms:'+atom['field']+':'+str(synonyms))
