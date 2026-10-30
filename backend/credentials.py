"""Backend-only credentials. Environment overrides a user-bound Windows DPAPI blob."""
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path

def encrypted_credentials(name,label):
    path=Path(__file__).resolve().parents[2]/'.runtime'/'secrets'/f'{name}.dpapi'
    if not path.exists() or os.name!='nt':raise ValueError(f'{label} credentials are not configured on the backend.')
    class Blob(ctypes.Structure):
        _fields_=[('size',wintypes.DWORD),('data',ctypes.POINTER(ctypes.c_ubyte))]
    payload=path.read_bytes();buffer=ctypes.create_string_buffer(payload)
    source=Blob(len(payload),ctypes.cast(buffer,ctypes.POINTER(ctypes.c_ubyte)));result=Blob()
    if not ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(source),None,None,None,None,1,ctypes.byref(result)):
        raise ValueError(f'The encrypted {label} credentials cannot be opened by this Windows user.')
    try:data=json.loads(ctypes.string_at(result.data,result.size))
    finally:ctypes.windll.kernel32.LocalFree(result.data)
    return data

def alpaca_credentials():
    key=os.environ.get('ALPACA_API_KEY');secret=os.environ.get('ALPACA_SECRET_KEY')
    if key and secret:return key,secret
    data=encrypted_credentials('alpaca','Alpaca')
    return data['key'],data['secret']

def twelvedata_key():
    return os.environ.get('TWELVEDATA_API_KEY') or encrypted_credentials('twelvedata','Twelve Data')['key']

def nasdaq_key():
    return os.environ.get('NASDAQ_DATA_LINK_API_KEY') or encrypted_credentials('nasdaq','Nasdaq Data Link')['key']
