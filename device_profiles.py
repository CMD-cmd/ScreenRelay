"""Validated device profiles; no display or controller side effects."""
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import unicodedata
import uuid

CATALOG = [{'id': 'ipad-pro-13-m4',
  'label': 'iPad Pro 13 (M4)',
  'native_width': 2752,
  'native_height': 2064,
  'ppi': 264,
  'source_url': 'https://support.apple.com/en-us/119891',
  'aliases': ['iPad Pro (13 英寸, M4)', 'iPad Pro 13-inch (M4)', 'iPad Pro 13 (M4)', '13 英寸 iPad Pro (M4)'],
  'hardware': 'tablet_long_camera'},
 {'id': 'ipad-pro-11-m4',
  'label': 'iPad Pro 11 (M4)',
  'native_width': 2420,
  'native_height': 1668,
  'ppi': 264,
  'source_url': 'https://support.apple.com/en-us/119892',
  'aliases': ['iPad Pro (11 英寸, M4)', 'iPad Pro 11-inch (M4)', 'iPad Pro 11 (M4)', '11 英寸 iPad Pro (M4)'],
  'hardware': 'tablet_long_camera'},
 {'id': 'ipad-air-13-m2',
  'label': 'iPad Air 13 (M2)',
  'native_width': 2732,
  'native_height': 2048,
  'ppi': 264,
  'source_url': 'https://support.apple.com/en-us/119893',
  'aliases': ['iPad Air (13 英寸, M2)', 'iPad Air 13-inch (M2)', 'iPad Air 13 (M2)', '13 英寸 iPad Air (M2)'],
  'hardware': 'tablet_long_camera'},
 {'id': 'ipad-mini-a17-pro',
  'label': 'iPad mini (A17 Pro)',
  'native_width': 2266,
  'native_height': 1488,
  'ppi': 326,
  'source_url': 'https://support.apple.com/en-us/121456',
  'aliases': ['iPad mini (A17 Pro)',
              'iPad mini A17 Pro',
              'iPad mini (7th generation)',
              'iPad mini（第 7 代）',
              'iPad mini 7'],
  'hardware': 'tablet_short_camera'},
 {'id': 'iphone-12',
  'label': 'iPhone 12',
  'native_width': 2532,
  'native_height': 1170,
  'ppi': 460,
  'source_url': 'https://support.apple.com/en-us/111876',
  'aliases': [],
  'hardware': 'wide_notch'},
 {'id': 'iphone-12-mini',
  'label': 'iPhone 12 mini',
  'native_width': 2340,
  'native_height': 1080,
  'ppi': 476,
  'source_url': 'https://support.apple.com/en-us/111877',
  'aliases': [],
  'hardware': 'wide_notch'},
 {'id': 'iphone-12-pro',
  'label': 'iPhone 12 Pro',
  'native_width': 2532,
  'native_height': 1170,
  'ppi': 460,
  'source_url': 'https://support.apple.com/en-us/111875',
  'aliases': [],
  'hardware': 'wide_notch'},
 {'id': 'iphone-12-pro-max',
  'label': 'iPhone 12 Pro Max',
  'native_width': 2778,
  'native_height': 1284,
  'ppi': 458,
  'source_url': 'https://support.apple.com/en-us/111874',
  'aliases': [],
  'hardware': 'wide_notch'},
 {'id': 'iphone-13',
  'label': 'iPhone 13',
  'native_width': 2532,
  'native_height': 1170,
  'ppi': 460,
  'source_url': 'https://support.apple.com/en-us/111872',
  'aliases': [],
  'hardware': 'compact_notch'},
 {'id': 'iphone-13-mini',
  'label': 'iPhone 13 mini',
  'native_width': 2340,
  'native_height': 1080,
  'ppi': 476,
  'source_url': 'https://support.apple.com/en-us/111873',
  'aliases': [],
  'hardware': 'compact_notch'},
 {'id': 'iphone-13-pro',
  'label': 'iPhone 13 Pro',
  'native_width': 2532,
  'native_height': 1170,
  'ppi': 460,
  'source_url': 'https://support.apple.com/en-us/111871',
  'aliases': [],
  'hardware': 'compact_notch'},
 {'id': 'iphone-13-pro-max',
  'label': 'iPhone 13 Pro Max',
  'native_width': 2778,
  'native_height': 1284,
  'ppi': 458,
  'source_url': 'https://support.apple.com/en-us/111870',
  'aliases': [],
  'hardware': 'compact_notch'},
 {'id': 'iphone-14',
  'label': 'iPhone 14',
  'native_width': 2532,
  'native_height': 1170,
  'ppi': 460,
  'source_url': 'https://support.apple.com/en-us/111850',
  'aliases': [],
  'hardware': 'compact_notch'},
 {'id': 'iphone-14-plus',
  'label': 'iPhone 14 Plus',
  'native_width': 2778,
  'native_height': 1284,
  'ppi': 458,
  'source_url': 'https://support.apple.com/en-us/111854',
  'aliases': [],
  'hardware': 'compact_notch'},
 {'id': 'iphone-14-pro',
  'label': 'iPhone 14 Pro',
  'native_width': 2556,
  'native_height': 1179,
  'ppi': 460,
  'source_url': 'https://support.apple.com/en-us/111849',
  'aliases': [],
  'hardware': 'dynamic_island'},
 {'id': 'iphone-14-pro-max',
  'label': 'iPhone 14 Pro Max',
  'native_width': 2796,
  'native_height': 1290,
  'ppi': 460,
  'source_url': 'https://support.apple.com/en-us/111846',
  'aliases': [],
  'hardware': 'dynamic_island'},
 {'id': 'iphone-15',
  'label': 'iPhone 15',
  'native_width': 2556,
  'native_height': 1179,
  'ppi': 460,
  'source_url': 'https://support.apple.com/en-us/111831',
  'aliases': [],
  'hardware': 'dynamic_island'},
 {'id': 'iphone-15-plus',
  'label': 'iPhone 15 Plus',
  'native_width': 2796,
  'native_height': 1290,
  'ppi': 460,
  'source_url': 'https://support.apple.com/en-us/111830',
  'aliases': [],
  'hardware': 'dynamic_island'},
 {'id': 'iphone-15-pro',
  'label': 'iPhone 15 Pro',
  'native_width': 2556,
  'native_height': 1179,
  'ppi': 460,
  'source_url': 'https://support.apple.com/en-us/111829',
  'aliases': [],
  'hardware': 'dynamic_island'},
 {'id': 'iphone-15-pro-max',
  'label': 'iPhone 15 Pro Max',
  'native_width': 2796,
  'native_height': 1290,
  'ppi': 460,
  'source_url': 'https://support.apple.com/en-us/111828',
  'aliases': [],
  'hardware': 'dynamic_island'},
 {'id': 'iphone-16',
  'label': 'iPhone 16',
  'native_width': 2556,
  'native_height': 1179,
  'ppi': 460,
  'source_url': 'https://support.apple.com/en-us/121029',
  'aliases': [],
  'hardware': 'dynamic_island'},
 {'id': 'iphone-16-plus',
  'label': 'iPhone 16 Plus',
  'native_width': 2796,
  'native_height': 1290,
  'ppi': 460,
  'source_url': 'https://support.apple.com/en-us/121030',
  'aliases': [],
  'hardware': 'dynamic_island'},
 {'id': 'iphone-16-pro',
  'label': 'iPhone 16 Pro',
  'native_width': 2622,
  'native_height': 1206,
  'ppi': 460,
  'source_url': 'https://support.apple.com/en-us/121031',
  'aliases': [],
  'hardware': 'dynamic_island'},
 {'id': 'iphone-16-pro-max',
  'label': 'iPhone 16 Pro Max',
  'native_width': 2868,
  'native_height': 1320,
  'ppi': 460,
  'source_url': 'https://support.apple.com/en-us/121032',
  'aliases': [],
  'hardware': 'dynamic_island'},
 {'id': 'iphone-16e',
  'label': 'iPhone 16e',
  'native_width': 2532,
  'native_height': 1170,
  'ppi': 460,
  'source_url': 'https://support.apple.com/en-us/122208',
  'aliases': [],
  'hardware': 'compact_notch'},
 {'id': 'iphone-17',
  'label': 'iPhone 17',
  'native_width': 2622,
  'native_height': 1206,
  'ppi': 460,
  'source_url': 'https://support.apple.com/en-us/125089',
  'aliases': [],
  'hardware': 'dynamic_island'},
 {'id': 'iphone-17-pro',
  'label': 'iPhone 17 Pro',
  'native_width': 2622,
  'native_height': 1206,
  'ppi': 460,
  'source_url': 'https://support.apple.com/en-us/125090',
  'aliases': [],
  'hardware': 'dynamic_island'},
 {'id': 'iphone-17-pro-max',
  'label': 'iPhone 17 Pro Max',
  'native_width': 2868,
  'native_height': 1320,
  'ppi': 460,
  'source_url': 'https://support.apple.com/en-us/125091',
  'aliases': [],
  'hardware': 'dynamic_island'},
 {'id': 'iphone-air',
  'label': 'iPhone Air',
  'native_width': 2736,
  'native_height': 1260,
  'ppi': 460,
  'source_url': 'https://support.apple.com/en-us/125092',
  'aliases': [],
  'hardware': 'dynamic_island'},
 {'id': 'iphone-xr',
  'label': 'iPhone XR',
  'native_width': 1792,
  'native_height': 828,
  'ppi': 326,
  'source_url': 'https://support.apple.com/en-us/111868',
  'aliases': [],
  'hardware': 'wide_notch'},
 {'id': 'iphone-xs',
  'label': 'iPhone XS',
  'native_width': 2436,
  'native_height': 1125,
  'ppi': 458,
  'source_url': 'https://support.apple.com/en-us/111881',
  'aliases': [],
  'hardware': 'wide_notch'},
 {'id': 'iphone-xs-max',
  'label': 'iPhone XS Max',
  'native_width': 2688,
  'native_height': 1242,
  'ppi': 458,
  'source_url': 'https://support.apple.com/en-us/111880',
  'aliases': [],
  'hardware': 'wide_notch'},
 {'id': 'iphone-11',
  'label': 'iPhone 11',
  'native_width': 1792,
  'native_height': 828,
  'ppi': 326,
  'source_url': 'https://support.apple.com/en-us/111865',
  'aliases': [],
  'hardware': 'wide_notch'},
 {'id': 'iphone-11-pro',
  'label': 'iPhone 11 Pro',
  'native_width': 2436,
  'native_height': 1125,
  'ppi': 458,
  'source_url': 'https://support.apple.com/en-us/111879',
  'aliases': [],
  'hardware': 'wide_notch'},
 {'id': 'iphone-11-pro-max',
  'label': 'iPhone 11 Pro Max',
  'native_width': 2688,
  'native_height': 1242,
  'ppi': 458,
  'source_url': 'https://support.apple.com/en-us/111878',
  'aliases': [],
  'hardware': 'wide_notch'},
 {'id': 'iphone-se-2',
  'label': 'iPhone SE (2nd generation)',
  'native_width': 1334,
  'native_height': 750,
  'ppi': 326,
  'source_url': 'https://support.apple.com/en-us/111882',
  'aliases': ['iPhone SE 2', 'iPhone SE (2020)', 'iPhone SE 第2代', 'iPhone SE (第 2 代)'],
  'hardware': 'phone_home_button'},
 {'id': 'iphone-se-3',
  'label': 'iPhone SE (3rd generation)',
  'native_width': 1334,
  'native_height': 750,
  'ppi': 326,
  'source_url': 'https://support.apple.com/en-us/111866',
  'aliases': ['iPhone SE 3', 'iPhone SE (2022)', 'iPhone SE 第3代', 'iPhone SE (第 3 代)'],
  'hardware': 'phone_home_button'},
 {'id': 'iphone-17e',
  'label': 'iPhone 17e',
  'native_width': 2532,
  'native_height': 1170,
  'ppi': 460,
  'source_url': 'https://support.apple.com/en-us/126470',
  'aliases': [],
  'hardware': 'compact_notch'},
 {'id': 'iphone-18-pro',
  'label': 'iPhone 18 Pro',
  'native_width': 2622,
  'native_height': 1206,
  'ppi': 460,
  'source_url': 'https://support.apple.com/en-us/148590',
  'aliases': [],
  'hardware': 'dynamic_island'},
 {'id': 'iphone-18-pro-max',
  'label': 'iPhone 18 Pro Max',
  'native_width': 2868,
  'native_height': 1320,
  'ppi': 460,
  'source_url': 'https://support.apple.com/en-us/148591',
  'aliases': [],
  'hardware': 'dynamic_island'},
 {'id': 'iphone-duo',
  'label': 'iPhone Duo',
  'native_width': 2034,
  'native_height': 1398,
  'ppi': 460,
  'source_url': 'https://support.apple.com/en-us/148588',
  'aliases': [],
  'hardware': 'dynamic_island',
  'display_variants': [{'id': 'outer',
                        'native_width': 2034,
                        'native_height': 1398,
                        'ppi': 460,
                        'hardware': 'dynamic_island',
                        'source_url': 'https://support.apple.com/en-us/148588',
                        'label': '外屏'},
                       {'id': 'inner',
                        'native_width': 2670,
                        'native_height': 1878,
                        'ppi': 430,
                        'hardware': 'dynamic_island',
                        'source_url': 'https://support.apple.com/en-us/148588',
                        'label': '内屏'}]},
 {'id': 'ipad-7',
  'label': 'iPad (7th generation)',
  'native_width': 2160,
  'native_height': 1620,
  'ppi': 264,
  'source_url': 'https://support.apple.com/en-us/111911',
  'aliases': ['iPad（第 7 代）', 'iPad 7', 'iPad (7th Gen)'],
  'hardware': 'tablet_home_button'},
 {'id': 'ipad-8',
  'label': 'iPad (8th generation)',
  'native_width': 2160,
  'native_height': 1620,
  'ppi': 264,
  'source_url': 'https://support.apple.com/en-us/118451',
  'aliases': ['iPad（第 8 代）', 'iPad 8', 'iPad (8th Gen)'],
  'hardware': 'tablet_home_button'},
 {'id': 'ipad-9',
  'label': 'iPad (9th generation)',
  'native_width': 2160,
  'native_height': 1620,
  'ppi': 264,
  'source_url': 'https://support.apple.com/en-us/111898',
  'aliases': ['iPad（第 9 代）', 'iPad 9', 'iPad (9th Gen)'],
  'hardware': 'tablet_home_button'},
 {'id': 'ipad-10',
  'label': 'iPad (10th generation)',
  'native_width': 2360,
  'native_height': 1640,
  'ppi': 264,
  'source_url': 'https://support.apple.com/en-us/111840',
  'aliases': ['iPad（第 10 代）', 'iPad 10', 'iPad (10th Gen)'],
  'hardware': 'tablet_long_camera'},
 {'id': 'ipad-a16',
  'label': 'iPad (A16)',
  'native_width': 2360,
  'native_height': 1640,
  'ppi': 264,
  'source_url': 'https://support.apple.com/en-us/122240',
  'aliases': ['iPad A16', 'iPad（A16）', 'iPad (11th generation)', 'iPad（第 11 代）'],
  'hardware': 'tablet_long_camera'},
 {'id': 'ipad-mini-5',
  'label': 'iPad mini (5th generation)',
  'native_width': 2048,
  'native_height': 1536,
  'ppi': 326,
  'source_url': 'https://support.apple.com/en-us/111904',
  'aliases': ['iPad mini（第 5 代）', 'iPad mini 5', 'iPad mini (5th Gen)'],
  'hardware': 'tablet_home_button'},
 {'id': 'ipad-mini-6',
  'label': 'iPad mini (6th generation)',
  'native_width': 2266,
  'native_height': 1488,
  'ppi': 326,
  'source_url': 'https://support.apple.com/en-us/111886',
  'aliases': ['iPad mini（第 6 代）', 'iPad mini 6', 'iPad mini (6th Gen)'],
  'hardware': 'tablet_short_camera'},
 {'id': 'ipad-air-3',
  'label': 'iPad Air (3rd generation)',
  'native_width': 2224,
  'native_height': 1668,
  'ppi': 264,
  'source_url': 'https://support.apple.com/en-us/111939',
  'aliases': ['iPad Air（第 3 代）', 'iPad Air 3', 'iPad Air (3rd Gen)'],
  'hardware': 'tablet_home_button'},
 {'id': 'ipad-air-4',
  'label': 'iPad Air (4th generation)',
  'native_width': 2360,
  'native_height': 1640,
  'ppi': 264,
  'source_url': 'https://support.apple.com/en-us/111905',
  'aliases': ['iPad Air（第 4 代）', 'iPad Air 4', 'iPad Air (4th Gen)'],
  'hardware': 'tablet_short_camera'},
 {'id': 'ipad-air-5',
  'label': 'iPad Air (5th generation)',
  'native_width': 2360,
  'native_height': 1640,
  'ppi': 264,
  'source_url': 'https://support.apple.com/en-us/111887',
  'aliases': ['iPad Air（第 5 代）', 'iPad Air 5', 'iPad Air (5th Gen)'],
  'hardware': 'tablet_short_camera'},
 {'id': 'ipad-air-11-m2',
  'label': 'iPad Air 11-inch (M2)',
  'native_width': 2360,
  'native_height': 1640,
  'ppi': 264,
  'source_url': 'https://support.apple.com/en-us/119894',
  'aliases': ['11 英寸 iPad Air (M2)', 'iPad Air (11 英寸, M2)', 'iPad Air 11 (M2)'],
  'hardware': 'tablet_long_camera'},
 {'id': 'ipad-air-11-m3',
  'label': 'iPad Air 11-inch (M3)',
  'native_width': 2360,
  'native_height': 1640,
  'ppi': 264,
  'source_url': 'https://support.apple.com/en-us/122241',
  'aliases': ['11 英寸 iPad Air (M3)', 'iPad Air (11 英寸, M3)', 'iPad Air 11 (M3)'],
  'hardware': 'tablet_long_camera'},
 {'id': 'ipad-air-13-m3',
  'label': 'iPad Air 13-inch (M3)',
  'native_width': 2732,
  'native_height': 2048,
  'ppi': 264,
  'source_url': 'https://support.apple.com/en-us/122242',
  'aliases': ['13 英寸 iPad Air (M3)', 'iPad Air (13 英寸, M3)', 'iPad Air 13 (M3)'],
  'hardware': 'tablet_long_camera'},
 {'id': 'ipad-air-11-m4',
  'label': 'iPad Air 11-inch (M4)',
  'native_width': 2360,
  'native_height': 1640,
  'ppi': 264,
  'source_url': 'https://support.apple.com/en-us/126471',
  'aliases': ['11 英寸 iPad Air (M4)', 'iPad Air (11 英寸, M4)', 'iPad Air 11 (M4)'],
  'hardware': 'tablet_long_camera'},
 {'id': 'ipad-air-13-m4',
  'label': 'iPad Air 13-inch (M4)',
  'native_width': 2732,
  'native_height': 2048,
  'ppi': 264,
  'source_url': 'https://support.apple.com/en-us/126472',
  'aliases': ['13 英寸 iPad Air (M4)', 'iPad Air (13 英寸, M4)', 'iPad Air 13 (M4)'],
  'hardware': 'tablet_long_camera'},
 {'id': 'ipad-pro-11-1',
  'label': 'iPad Pro 11-inch (1st generation)',
  'native_width': 2388,
  'native_height': 1668,
  'ppi': 264,
  'source_url': 'https://support.apple.com/en-us/111974',
  'aliases': ['11 英寸 iPad Pro（第 1 代）',
              'iPad Pro (11 英寸) (第 1 代)',
              'iPad Pro 11-inch (1st Gen)',
              'iPad Pro 11-inch (2018)'],
  'hardware': 'tablet_short_camera'},
 {'id': 'ipad-pro-11-2',
  'label': 'iPad Pro 11-inch (2nd generation)',
  'native_width': 2388,
  'native_height': 1668,
  'ppi': 264,
  'source_url': 'https://support.apple.com/en-us/118452',
  'aliases': ['11 英寸 iPad Pro（第 2 代）',
              'iPad Pro (11 英寸) (第 2 代)',
              'iPad Pro 11-inch (2nd Gen)',
              'iPad Pro 11-inch (2020)'],
  'hardware': 'tablet_short_camera'},
 {'id': 'ipad-pro-11-3',
  'label': 'iPad Pro 11-inch (3rd generation)',
  'native_width': 2388,
  'native_height': 1668,
  'ppi': 264,
  'source_url': 'https://support.apple.com/en-us/111897',
  'aliases': ['11 英寸 iPad Pro（第 3 代）',
              'iPad Pro (11 英寸) (第 3 代)',
              'iPad Pro 11-inch (3rd Gen)',
              'iPad Pro 11-inch (2021)'],
  'hardware': 'tablet_short_camera'},
 {'id': 'ipad-pro-11-4',
  'label': 'iPad Pro 11-inch (4th generation)',
  'native_width': 2388,
  'native_height': 1668,
  'ppi': 264,
  'source_url': 'https://support.apple.com/en-us/111842',
  'aliases': ['11 英寸 iPad Pro（第 4 代）',
              'iPad Pro (11 英寸) (第 4 代)',
              'iPad Pro 11-inch (4th Gen)',
              'iPad Pro 11-inch (2022)'],
  'hardware': 'tablet_short_camera'},
 {'id': 'ipad-pro-12-9-3',
  'label': 'iPad Pro 12.9-inch (3rd generation)',
  'native_width': 2732,
  'native_height': 2048,
  'ppi': 264,
  'source_url': 'https://support.apple.com/en-us/111979',
  'aliases': ['12.9 英寸 iPad Pro（第 3 代）',
              'iPad Pro (12.9 英寸) (第 3 代)',
              'iPad Pro 12.9-inch (3rd Gen)',
              'iPad Pro 12.9-inch (2018)'],
  'hardware': 'tablet_short_camera'},
 {'id': 'ipad-pro-12-9-4',
  'label': 'iPad Pro 12.9-inch (4th generation)',
  'native_width': 2732,
  'native_height': 2048,
  'ppi': 264,
  'source_url': 'https://support.apple.com/en-us/111977',
  'aliases': ['12.9 英寸 iPad Pro（第 4 代）',
              'iPad Pro (12.9 英寸) (第 4 代)',
              'iPad Pro 12.9-inch (4th Gen)',
              'iPad Pro 12.9-inch (2020)'],
  'hardware': 'tablet_short_camera'},
 {'id': 'ipad-pro-12-9-5',
  'label': 'iPad Pro 12.9-inch (5th generation)',
  'native_width': 2732,
  'native_height': 2048,
  'ppi': 264,
  'source_url': 'https://support.apple.com/en-us/111896',
  'aliases': ['12.9 英寸 iPad Pro（第 5 代）',
              'iPad Pro (12.9 英寸) (第 5 代)',
              'iPad Pro 12.9-inch (5th Gen)',
              'iPad Pro 12.9-inch (2021)'],
  'hardware': 'tablet_short_camera'},
 {'id': 'ipad-pro-12-9-6',
  'label': 'iPad Pro 12.9-inch (6th generation)',
  'native_width': 2732,
  'native_height': 2048,
  'ppi': 264,
  'source_url': 'https://support.apple.com/en-us/111841',
  'aliases': ['12.9 英寸 iPad Pro（第 6 代）',
              'iPad Pro (12.9 英寸) (第 6 代)',
              'iPad Pro 12.9-inch (6th Gen)',
              'iPad Pro 12.9-inch (2022)'],
  'hardware': 'tablet_short_camera'},
 {'id': 'ipad-pro-11-m5',
  'label': 'iPad Pro 11-inch (M5)',
  'native_width': 2420,
  'native_height': 1668,
  'ppi': 264,
  'source_url': 'https://support.apple.com/en-us/125406',
  'aliases': ['11 英寸 iPad Pro (M5)', 'iPad Pro (11 英寸, M5)', 'iPad Pro 11 (M5)'],
  'hardware': 'tablet_long_camera'},
 {'id': 'ipad-pro-13-m5',
  'label': 'iPad Pro 13-inch (M5)',
  'native_width': 2752,
  'native_height': 2064,
  'ppi': 264,
  'source_url': 'https://support.apple.com/en-us/125407',
  'aliases': ['13 英寸 iPad Pro (M5)', 'iPad Pro (13 英寸, M5)', 'iPad Pro 13 (M5)'],
  'hardware': 'tablet_long_camera'},
 {'id': 'ipad-mini-a20-pro',
  'label': 'iPad mini (A20 Pro)',
  'native_width': 2316,
  'native_height': 1512,
  'ppi': 326,
  'source_url': 'https://www.apple.com/ipad-mini/specs/',
  'aliases': ['iPad mini A20 Pro', 'iPad mini (8th generation)', 'iPad mini（第 8 代）', 'iPad mini 8'],
  'hardware': 'tablet_long_camera',
  'availability': 'announced_preorder_not_yet_shipping',
  'first_availability_date': '2026-11-06'}]
LEGACY_ID = 'ipad-pro-13-m4'
LEGACY_UUID = '00000000-0000-4000-8000-000000000001'
LEGACY_IDENTITY = 'virtual:2198:55152:1000000001'


def _text(value, field, max_length=160):
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise ValueError(field + ' must be a nonempty bounded string')
    if any(unicodedata.category(c).startswith('C') for c in value):
        raise ValueError(field + ' contains control characters')
    return value.strip()


def _id(value):
    value = _text(value, 'id', 100)
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]*', value):
        raise ValueError('invalid profile id')
    return value


def _number(value, field, low, high, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(field + ' must be numeric')
    if not low <= value <= high or not math.isfinite(value) or (integer and int(value) != value):
        raise ValueError(field + ' is out of range')
    return int(value) if integer else float(value)


def normalize_name(name):
    name = unicodedata.normalize('NFKC', _text(name, 'device name', 300)).casefold()
    # Discard typography only; generation and chip tokens remain significant.
    normalized = ''.join(c for c in name if c.isalnum())
    if not normalized:
        raise ValueError('device name contains no significant characters')
    return normalized


def _catalog(catalog_id):
    for item in CATALOG:
        if item['id'] == catalog_id:
            return item
    raise ValueError('unknown catalog_id')


def catalog_spec(catalog_id, display_variant=None):
    item = _catalog(catalog_id)
    variants = item.get('display_variants', [])
    if not variants:
        if display_variant is not None:
            raise ValueError('this model has one display specification')
        return item
    variant_id = variants[0]['id'] if display_variant is None else display_variant
    variant = next((v for v in variants if v['id'] == variant_id), None)
    if variant is None:
        raise ValueError('unknown display variant')
    return {**item, **{k: variant[k] for k in ('native_width', 'native_height', 'ppi', 'hardware')},
            'display_variant': variant_id}


def profile_snapshot(profile):
    result = copy.deepcopy(profile)
    spec = catalog_spec(profile['catalog_id'], profile.get('display_variant')) if profile.get('catalog_id') else {}
    result['hardware'] = spec.get('hardware', 'generic')
    if spec.get('display_variants'):
        result['display_variants'] = copy.deepcopy(spec['display_variants'])
        result['display_variant'] = spec['display_variant']
    return result


def _virtual(profile_id, label):
    digest = hashlib.sha256(profile_id.encode('utf-8')).digest()
    vendor, model = 2198, int.from_bytes(digest[:2], 'big') or 1
    serial = int.from_bytes(digest[2:6], 'big') or 1
    return dict(name='UU · ' + label + ' · ' + digest.hex()[:10], vendor=vendor, model=model, serial=serial,
                identity=f'virtual:{vendor}:{model}:{serial}')


def legacy_profile(preferred_resolution='1280x960', cursor_scale=3):
    return validate_profile(dict(id=LEGACY_ID, label='iPad Pro 13 (M4)', catalog_id=LEGACY_ID,
        native_width=2752, native_height=2064, ppi=264, orientation='landscape',
        resolution=preferred_resolution, cursor_scale=cursor_scale,
        virtual=dict(name='iPad Pro 13', vendor=2198, model=55152, serial=1000000001,
                     identity=LEGACY_IDENTITY, legacy_uuid=LEGACY_UUID)))


def make_profile(catalog_id=None, **overrides):
    allowed = {'id', 'label', 'native_width', 'native_height', 'ppi', 'orientation',
               'resolution', 'cursor_scale', 'virtual', 'display_area', 'display_variant'}
    if set(overrides) - allowed:
        raise ValueError('unknown profile fields')
    specification = None if catalog_id is None else catalog_spec(catalog_id, overrides.get('display_variant'))
    base = {} if specification is None else {k: v for k, v in specification.items()
                                        if k in ('label', 'native_width', 'native_height', 'ppi')}
    if catalog_id is not None:
        base['catalog_id'] = catalog_id
        if specification.get('display_variants'):
            base['display_variant'] = specification['display_variant']
    base.update(overrides)
    base.setdefault('id', 'device-' + uuid.uuid4().hex)
    base.setdefault('orientation', 'landscape')
    if not all(k in base for k in ('label', 'native_width', 'native_height', 'ppi')):
        raise ValueError('custom devices require label, native dimensions and ppi')
    base['id'] = _id(base['id'])
    base['label'] = _text(base['label'], 'label', 100)
    ppi = _number(base['ppi'], 'ppi', 50, 1000)
    base.setdefault('cursor_scale', min(4, max(1.5, math.floor(3 * ppi / 264 * 2 + .5) / 2)))
    base.setdefault('virtual', _virtual(base['id'], base['label']))
    if 'resolution' not in base:
        base['resolution'] = recommended_resolution(base)
    return validate_profile(base)


def _dimensions(profile, orientation=None):
    width = _number(profile.get('native_width'), 'native_width', 64, 16384, True)
    height = _number(profile.get('native_height'), 'native_height', 64, 16384, True)
    orientation = profile.get('orientation', 'landscape') if orientation is None else orientation
    if orientation not in ('landscape', 'portrait'):
        raise ValueError('invalid orientation')
    return (*sorted((width, height), reverse=orientation == 'landscape'), orientation)


def display_area_settings(profile):
    """Per-orientation preferences; older profiles retain their full canvas."""
    raw = profile.get('display_area', {})
    if not isinstance(raw, dict) or set(raw) - {'landscape', 'portrait'}:
        raise ValueError('invalid display area settings')
    result = {}
    for orientation in ('landscape', 'portrait'):
        item = raw.get(orientation, {})
        if not isinstance(item, dict) or set(item) - {'mode', 'inset_percent'}:
            raise ValueError('invalid display area preference')
        mode = item.get('mode', 'full')
        if mode not in ('full', 'safe'):
            raise ValueError('invalid display area mode')
        inset = _number(item.get('inset_percent', 7), 'inset_percent', 0, 12)
        if inset * 2 != round(inset * 2):
            raise ValueError('display area inset must use half-percent steps')
        result[orientation] = dict(mode=mode, inset_percent=inset)
    return result


def canvas_dimensions(profile, orientation=None):
    """The physical-pixel canvas sent to a centered, aspect-fit client."""
    width, height, orientation = _dimensions(profile, orientation)
    area = display_area_settings(profile)[orientation]
    if area['mode'] == 'safe':
        factor = 1 - area['inset_percent'] / 50
        if orientation == 'landscape':
            width = math.floor(width * factor / 2 + .5) * 2
        else:
            height = math.floor(height * factor / 2 + .5) * 2
        if min(width, height) < 64 or ((width < height) if orientation == 'landscape' else (height < width)):
            raise ValueError('display area inset exceeds the available long edge')
    return width, height, orientation


def fit_resolution(resolution, profile, orientation=None):
    """Keep the logical short edge (text scale) when changing the canvas."""
    width, height, orientation = canvas_dimensions(profile, orientation)
    rw, rh = map(int, resolution.split('x'))
    short = min(rw, rh, round(min(width, height) / 2))
    long = round(short * max(width, height) / min(width, height))
    return f'{long}x{short}' if orientation == 'landscape' else f'{short}x{long}'


def presets(profile, orientation=None):
    width, height, orientation = _dimensions(profile, orientation)
    virtual = profile.get('virtual', {})
    if virtual.get('identity') == LEGACY_IDENTITY:
        sizes = [(1376, 1032), (1280, 960), (1200, 900), (1152, 864), (1024, 768)]
        if orientation == 'portrait':
            sizes = [(h, w) for w, h in sizes]
    else:
        scales = (1, .9, .8, .744, 2/3) if is_phone(profile) else (1, .93, .875, .84, .744)
        sizes = [(max(1, round(width / 2 * scale)), max(1, round(height / 2 * scale)))
                 for scale in scales]
    modes = [f'{w}x{h}' for w, h in sizes]
    if canvas_dimensions(profile, orientation)[:2] != (width, height):
        modes = [fit_resolution(mode, profile, orientation) for mode in modes]
    return list(dict.fromkeys(modes))


def is_phone(profile):
    catalog_id = profile.get('catalog_id', profile.get('id', ''))
    return any(item['id'] == catalog_id and item['id'].startswith('iphone-') for item in CATALOG)


def recommended_resolution(profile):
    modes = presets(profile)
    return modes[min(2 if is_phone(profile) else 1, len(modes)-1)]


def catalog_snapshot():
    """Publish the same recommendations used by automatic device creation."""
    return [dict(spec, orientation='landscape', resolution=recommended_resolution(spec),
                 presets=presets(spec),
                 cursor_scale=min(4, max(1.5, math.floor(3 * spec['ppi'] / 264 * 2 + .5) / 2)))
            for spec in (catalog_spec(item['id']) for item in CATALOG)]


def validate_profile(profile):
    if not isinstance(profile, dict):
        raise ValueError('profile must be an object')
    allowed = {'id', 'label', 'catalog_id', 'native_width', 'native_height', 'ppi',
               'orientation', 'resolution', 'cursor_scale', 'virtual', 'display_area', 'display_variant'}
    if set(profile) - allowed:
        raise ValueError('unknown profile fields')
    required = allowed - {'catalog_id', 'display_area', 'display_variant'}
    if not required <= profile.keys():
        raise ValueError('missing profile fields')
    result = copy.deepcopy(profile)
    result['id'] = _id(result['id'])
    result['label'] = _text(result['label'], 'label', 100)
    if 'display_variant' in result and not isinstance(result['display_variant'], str):
        raise ValueError('display variant must identify a verified panel')
    if 'catalog_id' in result:
        catalog = catalog_spec(result['catalog_id'], result.get('display_variant'))
        if catalog.get('display_variants'):
            result['display_variant'] = catalog['display_variant']
        if any(result.get(key) != catalog[key] for key in ('native_width', 'native_height', 'ppi')):
            raise ValueError('catalog dimensions must match the verified specification')
    elif 'display_variant' in result:
        raise ValueError('display variant requires a catalog model')
    if 'display_area' in result:
        result['display_area'] = display_area_settings(result)
    width, height, orientation = canvas_dimensions(result)
    # Check both saved directions before persisting either preference.
    for other in ('landscape', 'portrait'):
        canvas_dimensions(result, other)
    for key in ('native_width', 'native_height'):
        result[key] = int(result[key])
    result['ppi'] = _number(result['ppi'], 'ppi', 50, 1000)
    result['cursor_scale'] = _number(result['cursor_scale'], 'cursor_scale', 1, 4)
    if result['cursor_scale'] not in (1, 1.5, 2, 2.5, 3, 3.5, 4):
        raise ValueError('cursor scale must use half steps')
    resolution = result['resolution']
    if not isinstance(resolution, str) or not re.fullmatch(r'[1-9][0-9]{0,4}x[1-9][0-9]{0,4}', resolution):
        raise ValueError('resolution must be WxH')
    rw, rh = map(int, resolution.split('x'))
    if min(rw, rh) < 16:
        raise ValueError('resolution is too small')
    if rw * 2 > width + 2 or rh * 2 > height + 2:
        raise ValueError('HiDPI resolution exceeds native pixels')
    if (orientation == 'landscape' and rw < rh) or (orientation == 'portrait' and rw > rh):
        raise ValueError('resolution orientation mismatch')
    if abs(rw / rh - width / height) > .005:
        raise ValueError('resolution aspect ratio mismatch')
    v = result['virtual']
    if not isinstance(v, dict) or set(v) - {'name', 'vendor', 'model', 'serial', 'identity', 'legacy_uuid'}:
        raise ValueError('invalid virtual descriptor')
    if not {'name', 'vendor', 'model', 'serial', 'identity'} <= v.keys():
        raise ValueError('missing virtual descriptor fields')
    v['name'] = _text(v['name'], 'virtual name')
    for key, limit in [('vendor', 65535), ('model', 65535), ('serial', 4294967295)]:
        v[key] = _number(v[key], key, 1, limit, True)
    if v['identity'] != f"virtual:{v['vendor']}:{v['model']}:{v['serial']}":
        raise ValueError('virtual identity mismatch')
    if 'legacy_uuid' in v:
        if v['legacy_uuid'] != LEGACY_UUID or v['identity'] != LEGACY_IDENTITY:
            raise ValueError('invalid legacy identity')
    return result


def preferred_mode(profile):
    p = validate_profile(profile)
    return dict(resolution=p['resolution'], hiDPI='on', refreshRate='60')


def target_spec(profile):
    p = validate_profile(profile)
    identities = [p['virtual']['identity']]
    if p['virtual'].get('legacy_uuid'):
        identities.append(p['virtual']['legacy_uuid'])
    return dict(name=p['virtual']['name'], identities=identities)


def _validate_config(config):
    required = {'version', 'automatic', 'selected_id', 'profiles', 'bindings'}
    if (not isinstance(config, dict) or not required <= set(config)
            or set(config) - required - {'binding_labels'}):
        raise ValueError('invalid profiles configuration fields')
    if type(config['version']) is not int or config['version'] != 1 or type(config['automatic']) is not bool:
        raise ValueError('invalid configuration version or automatic flag')
    if not isinstance(config['profiles'], list) or not config['profiles']:
        raise ValueError('profiles must be a nonempty list')
    result = copy.deepcopy(config)
    result['profiles'] = [validate_profile(p) for p in config['profiles']]
    ids = [p['id'] for p in result['profiles']]
    identities = [p['virtual']['identity'] for p in result['profiles']]
    if len(set(ids)) != len(ids) or len(set(identities)) != len(identities):
        raise ValueError('duplicate profile id or virtual identity')
    if not isinstance(config['selected_id'], str) or config['selected_id'] not in ids:
        raise ValueError('selected profile does not exist')
    if not isinstance(config['bindings'], dict):
        raise ValueError('bindings must be an object')
    bindings = {}
    for name, profile_id in config['bindings'].items():
        normalized = normalize_name(name)
        if not isinstance(profile_id, str) or profile_id not in ids:
            raise ValueError('binding profile does not exist')
        if normalized in bindings and bindings[normalized] != profile_id:
            raise ValueError('conflicting normalized bindings')
        bindings[normalized] = profile_id
    result['bindings'] = bindings
    labels = config.get('binding_labels', {})
    if not isinstance(labels, dict):
        raise ValueError('binding_labels must be an object')
    for key, label in labels.items():
        if key not in bindings or normalize_name(key) != key or normalize_name(label) != key:
            raise ValueError('binding label must match an existing normalized binding')
    if 'binding_labels' in config:
        result['binding_labels'] = copy.deepcopy(labels)
    return result


def identify(names, config):
    config = _validate_config(config)
    if not isinstance(names, list):
        raise ValueError('names must be a list')
    normalized = [normalize_name(n) for n in names]
    def decision(status, reason, **extra):
        return dict(status=status, profile_id=None, catalog_id=None, source='name', reason=reason, **extra)
    if not names:
        return decision('none', 'No connected controller name')
    if len(names) != 1:
        return decision('multiple', 'Multiple controller names, including duplicates, cannot identify one device')
    name = normalized[0]
    if name in config['bindings']:
        profile_id = config['bindings'][name]
        p = next(p for p in config['profiles'] if p['id'] == profile_id)
        return dict(status='matched', profile_id=profile_id, catalog_id=p.get('catalog_id'),
                    source='binding', reason='Explicit user binding')
    matches = [c for c in CATALOG if name in {normalize_name(a) for a in [c['label'], *c['aliases']]}]
    if len(matches) > 1:
        return decision('ambiguous', 'Name matches more than one catalog entry')
    if not matches:
        return decision('unknown', 'Name has no exact generation-specific catalog match')
    catalog_id = matches[0]['id']
    profiles = [p for p in config['profiles'] if p.get('catalog_id') == catalog_id]
    if len(profiles) > 1:
        return dict(status='ambiguous', profile_id=None, catalog_id=catalog_id,
                    source='name', reason='Multiple profiles match this catalog entry; bind the name explicitly')
    return dict(status='matched', profile_id=profiles[0]['id'] if profiles else None,
                catalog_id=catalog_id, source='name', reason='Exact generation-specific catalog name')


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON object key')
        result[key] = value
    return result


class ProfileStore:
    def __init__(self, directory):
        self.path = Path(directory) / 'profiles.json'

    def load(self):
        try:
            with self.path.open('r', encoding='utf-8') as stream:
                return _validate_config(json.load(stream, object_pairs_hook=_unique_object))
        except FileNotFoundError:
            return None

    def save(self, config):
        normalized = _validate_config(config)
        # An invalid existing file is evidence, never an implicit reset request.
        self.load()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', prefix='.profiles-',
                                             suffix='.tmp', dir=self.path.parent, delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(normalized, stream, ensure_ascii=False, indent=2, allow_nan=False)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            temporary = None
            fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return normalized

    def bootstrap(self, legacy_resolution='1280x960', scale=3):
        existing = self.load()
        if existing is not None:
            return existing
        profile = legacy_profile(legacy_resolution, scale)
        return self.save(dict(version=1, automatic=True, selected_id=profile['id'],
                              profiles=[profile], bindings={}))
