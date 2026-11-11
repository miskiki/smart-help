# -*- coding: utf-8 -*-
"""生成全国行政区划数据资产 data/regions/china_divisions.json（一次性构建脚本）。

数据源：https://github.com/modood/Administrative-divisions-of-China
        dist/pca.json（省 -> 市 -> 县区，名称级；MIT License）
说明：
- 只保留省/市/县区三级名称，不含街道；不包含港澳台（政策库当前口径为大陆 31 省）。
- 直辖市在源数据里是 省=北京市 -> 市=市辖区 -> 县区，这里保留原结构（市辖区 作为伪地级节点）。
- 省直辖县级行政区划（河南济源、湖北 4 个、海南 15 个）保留为伪地级节点，
  加载器会把它们的县区识别为"省直辖县级市"。
- 数据是公开行政区划名称表，不猜简称、不改写名称。
用法：
  python deploy/build_china_divisions.py            # 联网重建
  python deploy/build_china_divisions.py --check    # 只校验现有文件覆盖数
"""
import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'data' / 'regions' / 'china_divisions.json'
SRC = 'https://raw.githubusercontent.com/modood/Administrative-divisions-of-China/master/dist/pca.json'
PSEUDO_CITY = ('市辖区', '县', '省直辖县级行政区划')


def build():
    with urllib.request.urlopen(SRC, timeout=60) as resp:
        pca = json.loads(resp.read().decode('utf-8'))
    provinces = []
    for pname, cities in pca.items():
        rows = []
        for cname, counties in cities.items():
            rows.append({'name': cname, 'counties': list(counties)})
        provinces.append({'name': pname, 'cities': rows})
    data = {
        'source': 'https://github.com/modood/Administrative-divisions-of-China (dist/pca.json)',
        'license': 'MIT',
        'fetched_at': __import__('datetime').date.today().isoformat(),
        'province_count': len(provinces),
        'city_count': sum(len(p['cities']) for p in provinces),
        'county_count': sum(len(c['counties']) for p in provinces for c in p['cities']),
        'note': '省->市->县区 名称表；市辖区/县/省直辖县级行政区划 为源数据伪地级节点，加载器单独处理。',
        'provinces': provinces,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
    print('OK %s provinces=%d cities=%d counties=%d bytes=%d' % (
        OUT.relative_to(ROOT), data['province_count'], data['city_count'],
        data['county_count'], OUT.stat().st_size))


def check():
    data = json.loads(OUT.read_text(encoding='utf-8'))
    names = {p['name'] for p in data['provinces']}
    print('OK provinces=%d cities=%d counties=%d' % (
        data['province_count'], data['city_count'], data['county_count']))
    print('has 江西省:', '江西省' in names, '| has 北京市:', '北京市' in names)
    for p in data['provinces']:
        if p['name'] == '江西省':
            print('江西省 cities:', [c['name'] for c in p['cities']])
            for c in p['cities']:
                if c['name'] == '赣州市':
                    print('赣州市 counties:', c['counties'])


def main():
    parser = argparse.ArgumentParser(description='构建全国行政区划数据资产')
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    check() if args.check else build()


if __name__ == '__main__':
    main()
