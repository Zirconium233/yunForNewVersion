import random
import time
from gmssl import sm4
import hashlib
import base64
import requests
from Crypto.Util.Padding import pad, unpad
import json
import configparser

SM4_BLOCK_SIZE = 16
conf = configparser.ConfigParser()

class Login():
    def md5_encryption(data):
        md5 = hashlib.md5()  # 创建一个md5对象
        md5.update(data.encode('utf-8'))  # 使用utf-8编码数据
        return md5.hexdigest()  # 返回加密后的十六进制字符串
    def hex_to_bytes(hex_str):
        return bytes.fromhex(hex_str)
    def pkcs7_padding(data):
        return pad(data, SM4_BLOCK_SIZE)
    def pkcs7_unpadding(data):
        return unpad(data, SM4_BLOCK_SIZE)
    def sm4_encrypt(plaintext, key, iv=None, mode='ECB', padding='Pkcs7', output_format='Base64'):
        crypt_sm4 = sm4.CryptSM4()
        key = Login.hex_to_bytes(key)
        # 设置加密模式
        if mode == 'ECB':
            crypt_sm4.set_key(key, sm4.SM4_ENCRYPT)
        elif mode == 'CBC':
            iv = Login.hex_to_bytes(iv) if iv else None
            crypt_sm4.set_key(key, sm4.SM4_ENCRYPT, iv)
        # 数据填充
        if padding == 'Pkcs7':
            plaintext = Login.pkcs7_padding(plaintext.encode())
        # 加密操作
        if mode == 'ECB':
            ciphertext = crypt_sm4.crypt_ecb(plaintext)
        elif mode == 'CBC':
            ciphertext = crypt_sm4.crypt_cbc(plaintext)
        # 输出格式转换
        if output_format == 'Base64':
            return base64.b64encode(ciphertext).decode()
        elif output_format == 'Hex':
            return ciphertext.hex()
    def sm4_decrypt(ciphertext, key, iv=None, mode='ECB', padding='Pkcs7', input_format='Base64'):
        crypt_sm4 = sm4.CryptSM4()
        key = Login.hex_to_bytes(key)
        # 设置解密模式
        if mode == 'ECB':
            crypt_sm4.set_key(key, sm4.SM4_DECRYPT)
        elif mode == 'CBC':
            iv = Login.hex_to_bytes(iv) if iv else None
            crypt_sm4.set_key(key, sm4.SM4_DECRYPT, iv)
        # 输入格式转换
        if input_format == 'Base64':
            ciphertext = base64.b64decode(ciphertext)
        elif input_format == 'Hex':
            ciphertext = bytes.fromhex(ciphertext)
        # 解密操作
        if mode == 'ECB':
            plaintext = crypt_sm4.crypt_ecb(ciphertext)
        elif mode == 'CBC':
            plaintext = crypt_sm4.crypt_cbc(ciphertext)
        # 数据去填充
        #if padding == 'Pkcs7':
            #plaintext = pkcs7_unpadding(plaintext)
        return plaintext.decode()
    def main():
        
        utc = int(time.time())

        #读取ini
        conf.read('./config.ini', encoding='utf-8')

        #判断[Login]是否存在
        if 'Login' not in conf.sections():
            conf.add_section('Login')
            conf.set('Login', 'username', '')
            conf.set('Login', 'password', '')
            with open('./config.ini', 'w', encoding='utf-8') as f:
                conf.write(f)
        
        #判断school_id是否在[Yun]中
        if 'school_id' not in conf['Yun']:
            conf.set('Yun', 'school_id', '100')
            with open('./config.ini', 'w', encoding='utf-8') as f:
                conf.write(f)
        
        #读取ini配置
        username = conf.get('Login', 'username') or input('未找到用户名，请输入用户名：')
        password = conf.get('Login', 'password') or input('未找到密码，请输入密码：')
        iniDeviceId = conf.get('User', 'device_id')
        iniDeviceName = conf.get('User', 'device_name')
        iniuuid = conf.get('User', 'uuid')
        iniSysedition = conf.get('User', 'sys_edition')
        appedition = conf.get('Yun', 'app_edition')
        url = conf.get('Yun', 'school_host') + '/login/appLoginHGD'
        platform = conf.get('Yun', 'platform')
        schoolid = conf.get('Yun', 'school_id')

        if username != conf.get('Login', 'username'):
            conf.set('Login', 'username', username)
            with open('./config.ini', 'w', encoding='utf-8') as f:
                conf.write(f)
        if password != conf.get('Login', 'password'):
            conf.set('Login', 'password', password)
            with open('./config.ini', 'w', encoding='utf-8') as f:
                conf.write(f)
        #如果部分配置为空则随机生成
        if iniDeviceId != '':
            DeviceId = iniDeviceId
        else:
            DeviceId = str(random.randint(1000000000000000, 9999999999999999))

        if iniuuid != '':
            uuid = iniuuid
        else:
            uuid = DeviceId
        
        if iniDeviceName != '':
            DeviceName = iniDeviceName
        else:
            print('DeviceName为空 请输入希望使用的设备名\n留空则使用默认名')
            DeviceName = input() or 'Xiaomi'
        
        if iniSysedition != '':
            sys_edition = iniSysedition
        else:
            print('Sys_edition为空 请输入希望使用的设备名\n留空则使用14')
            sys_edition = input() or '14'

        
        #md5签名结果用hex
        encryptData = '''{"password":"'''+password+'''","schoolId":"'''+schoolid+'''","userName":"'''+username+'''","type":"1"}'''
        #签名结果
        sign_data='platform=android&utc={}&uuid={}&appsecret=pie0hDSfMRINRXc7s1UIXfkE'.format(utc,uuid)
        sign=Login.md5_encryption(sign_data)
        key='e2c9e15e84f93b81ee01bbd299a31563'
        content=Login.sm4_encrypt(encryptData, key, mode='ECB', padding='Pkcs7', output_format='Base64')
        content=content[:-24]
        headers = {
            "token": "",
            "isApp": "app",
            "deviceId": uuid,
            "deviceName": DeviceName,
            "version": appedition,
            "platform": platform,
            "uuid": uuid,
            "utc": str(utc),
            "sign": sign,
            "Content-Type": "application/json; charset=utf-8",
            "Accept-Encoding": "gzip",
            "User-Agent": "okhttp/3.12.0"
        }
        # 请求体内容
        data = {
            "cipherKey": "BL+FHB2+eDL3gMtv1+2UljBFraZYQFOXkmyKrqyRAzcw1R4rsq1i8p1tEOXhZMHTlFWmR+i/mdf4DNi0hCUSoQ88JMTUSUIkgU0+mowqRlVc/n/qYGqXERFqyMqn+GANUvWU65+F6/RLhpAB3AiYSJOY/RplvXmRvQ==",
            "content": content
        }
        # 发送POST请求
        response = requests.post(url, headers=headers, json=data)
        # 打印响应内容
        result=response.text
        rawResponse=json.dumps(json.loads(result))
        if rawResponse.find('500') != -1:
            print('返回数据报错 检查账号密码')
            exit()
        else:
            DecryptedData=json.loads(Login.sm4_decrypt(result,key, mode='ECB', padding='Pkcs7',input_format='Base64'))
        token=DecryptedData['data']['token']
        if response.status_code == 200:
            print("登录成功，本次登录尝试获得的token为：" + token + "  本次生成的uuid为：" + uuid)
            print("!请注意! 使用脚本登录后会导致手机客户端登录失效\n请尽量减少手机登录次数，避免被识别为多设备登录代跑")
        return token,DeviceId,DeviceName,uuid,sys_edition