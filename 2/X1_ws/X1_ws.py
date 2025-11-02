import _thread as thread
import base64
import datetime
import hashlib
import hmac
import json
from urllib.parse import urlparse
import ssl
from datetime import datetime
from time import mktime
from urllib.parse import urlencode
from wsgiref.handlers import format_date_time

import websocket  # 使用websocket_client
import os
answer = ""
isFirstcontent = False

class Ws_Param(object):
    # 初始化
    def __init__(self, APPID, APIKey, APISecret, Spark_url):
        self.APPID = APPID
        self.APIKey = APIKey
        self.APISecret = APISecret
        self.host = urlparse(Spark_url).netloc
        self.path = urlparse(Spark_url).path
        self.Spark_url = Spark_url

    # 生成url
    def create_url(self):
        # 生成RFC1123格式的时间戳
        now = datetime.now()
        date = format_date_time(mktime(now.timetuple()))
        # 校验必需参数
        if not self.APIKey or not self.APISecret or not self.APPID:
            raise ValueError("缺少 APIKey/APISecret/APPID，请通过环境变量或配置注入。")
        # 拼接字符串
        signature_origin = "host: " + self.host + "\n"
        signature_origin += "date: " + date + "\n"
        signature_origin += "GET " + self.path + " HTTP/1.1"
        # 进行hmac-sha256进行加密
        signature_sha = hmac.new(self.APISecret.encode('utf-8'), signature_origin.encode('utf-8'),
                                 digestmod=hashlib.sha256).digest()
        signature_sha_base64 = base64.b64encode(signature_sha).decode(encoding='utf-8')
        authorization_origin = f'api_key="{self.APIKey}", algorithm="hmac-sha256", headers="host date request-line", signature="{signature_sha_base64}"'
        authorization = base64.b64encode(authorization_origin.encode('utf-8')).decode(encoding='utf-8')
        v = {
            "authorization": authorization,
            "date": date,
            "host": self.host
        }
        url = self.Spark_url + '?' + urlencode(v)
        return url


# 收到websocket错误的处理
def on_error(ws, error):
    print("### error:", error)


# 收到websocket关闭的处理
def on_close(ws, close_status_code, close_msg):
    print(f"### closed ### code={close_status_code} msg={close_msg}")


# 收到websocket连接建立的处理
def on_open(ws):
    thread.start_new_thread(run, (ws,))


def run(ws, *args):
    data = json.dumps(gen_params(appid=ws.appid, domain= ws.domain,question=ws.question))
    ws.send(data)


# 收到websocket消息的处理
def on_message(ws, message):
    # print(message)
    data = json.loads(message)
    code = data['header']['code']
    content =''
    if code != 0:
        print(f'请求错误: {code}, {data}')
        ws.close()
    else:
        choices = data["payload"]["choices"]
        status = choices["status"]
        text = choices['text'][0]
        if ( 'reasoning_content' in text and '' != text['reasoning_content']):
            reasoning_content = text["reasoning_content"]
            print(reasoning_content, end="")
            global isFirstcontent
            isFirstcontent = True

        if('content' in text and '' != text['content']):
            content = text["content"]
            if(True == isFirstcontent):
                print("\n*******************以上为思维链内容，模型回复内容如下********************\n")
            print(content, end="")
            isFirstcontent = False
        global answer
        answer += content
        # print(1)
        if status == 2:
            ws.close()


def gen_params(appid, domain,question):
    """
    通过appid和用户的提问来生成请参数
    """
    data = {
        "header": {
            "app_id": appid,
            "uid": "1234",
        },
        "parameter": {
            "chat": {
                "domain": domain,
                "temperature": 1.2,
                "max_tokens": 32768       # 请根据不同模型支持范围，适当调整该值的大小
            }
        },
        "payload": {
            "message": {
                "text": question
            }
        }
    }
    return data


def main(appid, api_key, api_secret, Spark_url,domain, question):
    # print("星火:")
    wsParam = Ws_Param(appid, api_key, api_secret, Spark_url)
    websocket.enableTrace(False)
    wsUrl = wsParam.create_url()
    ws = websocket.WebSocketApp(wsUrl, on_message=on_message, on_error=on_error, on_close=on_close, on_open=on_open)
    ws.appid = appid
    ws.question = question
    ws.domain = domain
    ws.run_forever(sslopt={"cert_reqs": ssl.CERT_NONE})



text = []


# 管理对话历史，按序编为列表
def getText(role, content):
    jsoncon = {}
    jsoncon["role"] = role
    jsoncon["content"] = content
    text.append(jsoncon)
    return text

# 获取对话中的所有角色的content长度
def getlength(text):
    length = 0
    for content in text:
        temp = content["content"]
        leng = len(temp)
        length += leng
    return length

# 判断长度是否超长，当前限制8K tokens
def checklen(text):
    while (getlength(text) > 8000):
        del text[0]
    return text


if __name__ == '__main__':
    # 仅测试用，完成后请删除或改为环境变量
    appid = "6f53bf56"
    api_key = "a898cb76e7844669196db96113c0bab0"
    api_secret = "OGMwODEyZTM1ZWM5MWQyNzMxNDllNWRj"
    # 从环境变量读取，便于保密与排查
    # appid = os.getenv("XFYUN_APPID", "").strip()
    # api_secret = os.getenv("XFYUN_APISECRET", "").strip()
    # api_key = os.getenv("XFYUN_APIKEY", "").strip()
    # if not appid or not api_key or not api_secret:
    #     print("请设置环境变量 XFYUN_APPID / XFYUN_APIKEY / XFYUN_APISECRET 后重试。示例（PowerShell）：")
    #     print('$env:XFYUN_APPID="6f53bf56"; $env:XFYUN_APIKEY="a898cb76e7844669196db96113c0bab0"; $env:XFYUN_APISECRET="OGMwODEyZTM1ZWM5MWQyNzMxNDllNWRj"')
    #     raise SystemExit(1)
    domain = "x1"
    Spark_url = "wss://spark-api.xf-yun.com/v1/x1"
    while True:
        Input = input("\n我:")
        question = checklen(getText("user", Input))
        print("星火:", end="")
        wsParam = Ws_Param(appid, api_key, api_secret, Spark_url)
        websocket.enableTrace(False)
        wsUrl = wsParam.create_url()
        print("debug websocket url:", wsUrl)  # 仅用于本地调试，注意不要把日志上传或分享
        main(appid, api_key, api_secret, Spark_url, domain, question)
        getText("assistant", answer)