import os
import json
import base64
import ssl
import time
import wave
import threading
import numpy as np
from datetime import datetime
from time import mktime
from urllib.parse import urlencode, urlparse
from wsgiref.handlers import format_date_time
import hashlib
import hmac
from queue import Queue

from fastapi import FastAPI, UploadFile, File, Form, Request
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import uvicorn
import websocket
import soundfile
from resampy import resample

# 配置参数（请替换为你的有效密钥）
APPID = "6f53bf56"
APIKey = "a898cb76e7844669196db96113c0bab0"
APISecret = "OGMwODEyZTM1ZWM5MWQyNzMxNDllNWRj"

# 星火X1配置
SPARK_URL = "wss://spark-api.xf-yun.com/v1/x1"
SPARK_DOMAIN = "x1"

# 语音听写（ASR）配置
ASR_URL = "wss://iat-api.xfyun.cn/v2/iat"  # 中英文推荐地址
ASR_LANGUAGE = "zh_cn"  # 中文
ASR_DOMAIN = "iat"  # 日常用语
ASR_ACCENT = "mandarin"  # 普通话
ASR_VAD_EOS = 3000  # 后端点检测（毫秒）
ASR_DWA = "wpgs"  # 开启动态修正

# 消息队列
asr_queue = Queue()
tts_queue = Queue()
spark_queue = Queue()

# FastAPI配置
app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


# WebSocket连接管理类
class WebSocketClient:
    def __init__(self, url, on_message, on_error=None, on_close=None):
        self.url = url
        self.on_message = on_message
        self.on_error = on_error or (lambda ws, err: print(f"WebSocket错误: {err}"))
        self.on_close = on_close or (lambda ws, code, msg: print(f"WebSocket关闭: {code} {msg}"))
        self.ws = None
        self.thread = None

    def connect(self, timeout=5):
        """建立连接并等待成功"""
        self.ws = websocket.WebSocketApp(
            self.url,
            on_message=self.on_message,
            on_error=self.on_error,
            on_close=self.on_close
        )
        self.thread = threading.Thread(target=self._run_forever)
        self.thread.daemon = True
        self.thread.start()
        
        # 等待连接成功
        start_time = time.time()
        while time.time() - start_time < timeout:
            if self.ws.sock and self.ws.sock.connected:
                return True
            time.sleep(0.1)
        return False

    def _run_forever(self):
        """运行WebSocket"""
        try:
            self.ws.run_forever(sslopt={"cert_reqs": ssl.CERT_NONE})
        except Exception as e:
            print(f"WebSocket运行错误: {e}")

    def send(self, data):
        """发送数据"""
        if self.ws and self.ws.sock and self.ws.sock.connected:
            try:
                self.ws.send(data)
                return True
            except Exception as e:
                print(f"发送数据失败: {e}")
        return False

    def close(self):
        """关闭连接"""
        if self.ws:
            self.ws.close()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=1.0)


# 语音识别(ASR)
def asr_on_message(ws, message):
    try:
        msg = json.loads(message)
        # 错误处理
        if msg["code"] != 0:
            asr_queue.put(("error", f"ASR错误: {msg['message']} (错误码: {msg['code']})"))
            return
            
        data = msg.get("data", {})
        result = data.get("result", {})
        status = data.get("status", 0)
        
        # 处理动态修正（dwa=wpgs）
        pgs = result.get("pgs", "")
        rg = result.get("rg", [])
        ws_list = result.get("ws", [])
        
        # 提取识别文本
        text = ""
        for ws_item in ws_list:
            for cw in ws_item.get("cw", []):
                text += cw.get("w", "")
        
        # 发送识别结果（包含状态信息）
        asr_queue.put((
            "text", 
            {
                "text": text,
                "status": status,
                "pgs": pgs,
                "rg": rg,
                "is_final": status == 2  # 2表示最后一块结果
            }
        ))
        
    except Exception as e:
        asr_queue.put(("error", f"ASR解析错误: {str(e)}"))


def speech_to_text(audio_bytes):
    # 转换音频为16k单声道PCM
    pcm_bytes = convert_audio_to_16k_mono_pcm(audio_bytes)
    if not pcm_bytes:
        return "", "音频转换失败"

    # ASR鉴权参数生成
    class ASR_Ws_Param:
        def __init__(self):
            self.url = ASR_URL
            self.host = urlparse(self.url).netloc
            self.path = urlparse(self.url).path
            self.api_key = APIKey
            self.api_secret = APISecret

        def create_url(self):
            try:
                # 生成RFC1123格式的时间戳
                now = datetime.now()
                date = format_date_time(mktime(now.timetuple()))
                
                # 生成签名原始字符串
                signature_origin = (
                    f"host: {self.host}\ndate: {date}\nGET {self.path} HTTP/1.1"
                )
                
                # 计算HMAC-SHA256签名
                signature_sha = hmac.new(
                    self.api_secret.encode('utf-8'),
                    signature_origin.encode('utf-8'),
                    hashlib.sha256
                ).digest()
                signature = base64.b64encode(signature_sha).decode('utf-8')
                
                # 生成authorization参数
                authorization_origin = (
                    f'api_key="{self.api_key}", '
                    f'algorithm="hmac-sha256", '
                    f'headers="host date request-line", '
                    f'signature="{signature}"'
                )
                authorization = base64.b64encode(authorization_origin.encode('utf-8')).decode('utf-8')
                
                # 拼接最终URL
                url_params = urlencode({
                    "authorization": authorization,
                    "date": date,
                    "host": self.host
                })
                return f"{self.url}?{url_params}"
            except Exception as e:
                return f"ASR签名错误: {str(e)}"

    # 创建ASR参数并生成URL
    asr_param = ASR_Ws_Param()
    ws_url = asr_param.create_url()
    if "error" in ws_url:
        return "", ws_url

    # 建立WebSocket连接
    ws_client = WebSocketClient(ws_url, asr_on_message)
    if not ws_client.connect():
        return "", "ASR连接失败（握手失败）"

    try:
        # 音频分片参数（文档建议：16k PCM每次发送1280字节，间隔40ms）
        frame_size = 1280  # 每帧字节数
        interval = 0.04  # 发送间隔（秒）
        total_frames = len(pcm_bytes) // frame_size
        remaining_bytes = len(pcm_bytes) % frame_size

        # 发送首帧数据（包含common和business参数）
        first_frame = {
            "common": {"app_id": APPID},
            "business": {
                "language": ASR_LANGUAGE,
                "domain": ASR_DOMAIN,
                "accent": ASR_ACCENT,
                "vad_eos": ASR_VAD_EOS,
                "dwa": ASR_DWA,
                "ptt": 1,  # 开启标点
                "pcm": 1   # 标点位置控制
            },
            "data": {
                "status": 0,  # 第一帧
                "format": "audio/L16;rate=16000",
                "encoding": "raw",
                "audio": base64.b64encode(pcm_bytes[:frame_size]).decode()
            }
        }
        if not ws_client.send(json.dumps(first_frame)):
            return "", "发送首帧音频失败"

        # 发送中间帧
        for i in range(1, total_frames):
            start = i * frame_size
            end = start + frame_size
            frame_data = {
                "data": {
                    "status": 1,  # 中间帧
                    "format": "audio/L16;rate=16000",
                    "encoding": "raw",
                    "audio": base64.b64encode(pcm_bytes[start:end]).decode()
                }
            }
            if not ws_client.send(json.dumps(frame_data)):
                return "", f"发送第{i}帧音频失败"
            time.sleep(interval)

        # 发送剩余数据（如果有）
        if remaining_bytes > 0:
            start = total_frames * frame_size
            frame_data = {
                "data": {
                    "status": 1,  # 中间帧
                    "format": "audio/L16;rate=16000",
                    "encoding": "raw",
                    "audio": base64.b64encode(pcm_bytes[start:]).decode()
                }
            }
            ws_client.send(json.dumps(frame_data))
            time.sleep(interval)

        # 发送结束帧
        end_frame = {
            "data": {
                "status": 2  # 最后一帧
            }
        }
        ws_client.send(json.dumps(end_frame))

        # 收集识别结果（处理动态修正）
        result_text = ""
        error_msg = None
        is_complete = False
        # 存储历史结果用于动态修正
        history = []
        
        while not is_complete:
            if not asr_queue.empty():
                type_, content = asr_queue.get()
                if type_ == "error":
                    error_msg = content
                    break
                elif type_ == "text":
                    # 处理动态修正（替换或追加）
                    if content["pgs"] == "rpl" and content["rg"]:
                        # 替换历史结果中指定范围的内容
                        start_idx, end_idx = content["rg"]
                        history = history[:start_idx] + [content["text"]]
                    else:
                        # 追加结果
                        history.append(content["text"])
                    
                    # 更新完整文本
                    result_text = "".join(history)
                    
                    # 检查是否为最终结果
                    if content["is_final"]:
                        is_complete = True
            time.sleep(0.01)
        
        return result_text, error_msg
    finally:
        ws_client.close()


# 语音合成(TTS) - 优化为支持多线程和缓存
def tts_on_message(ws, message):
    try:
        msg = json.loads(message)
        if msg["code"] != 0:
            tts_queue.put(("error", f"TTS错误: {msg['message']}"))
            return
        audio = base64.b64decode(msg["data"]["audio"])
        tts_queue.put(("audio", audio))
        if msg["data"]["status"] == 2:
            tts_queue.put(("done", None))
    except Exception as e:
        tts_queue.put(("error", f"TTS解析错误: {str(e)}"))


def text_to_speech(text_content, cache_id=None):
    """生成语音并可选择缓存"""
    if not text_content:
        return None, "文本为空"

    class TTS_Ws_Param:
        def __init__(self, text):
            self.url = 'wss://tts-api.xfyun.cn/v2/tts'
            self.common = {"app_id": APPID}
            # 优化发音人参数，可根据需要更换
            self.business = {
                "aue": "raw", 
                "auf": "audio/L16;rate=16000", 
                "vcn": "xiaoyan",  # 女声
                # "vcn": "xiaoyu",  # 另一个女声
                # "vcn": "aisjiuxu",  # 男声
                "tte": "utf8",
                "speed": 50,  # 语速，0-100
                "volume": 50,  # 音量，0-100
                "pitch": 50    # 音调，0-100
            }
            self.data = {"status": 2, "text": base64.b64encode(text.encode()).decode()}

        def create_url(self):
            try:
                now = datetime.now()
                date = format_date_time(mktime(now.timetuple()))
                signature_origin = f"host: tts-api.xfyun.cn\ndate: {date}\nGET /v2/tts HTTP/1.1"
                signature_sha = hmac.new(APISecret.encode(), signature_origin.encode(), hashlib.sha256).digest()
                signature = base64.b64encode(signature_sha).decode()
                authorization = base64.b64encode(
                    f'api_key="{APIKey}", algorithm="hmac-sha256", headers="host date request-line", signature="{signature}"'.encode()
                ).decode()
                return f"{self.url}?authorization={authorization}&date={date}&host=tts-api.xfyun.cn"
            except Exception as e:
                return f"URL生成错误: {str(e)}"

    tts_param = TTS_Ws_Param(text_content)
    ws_url = tts_param.create_url()
    if "error" in ws_url:
        return None, ws_url

    ws_client = WebSocketClient(ws_url, tts_on_message)
    if not ws_client.connect():
        return None, "TTS连接失败（检查网络或URL）"

    try:
        if not ws_client.send(json.dumps({
            "common": tts_param.common,
            "business": tts_param.business,
            "data": tts_param.data
        })):
            return None, "发送TTS请求失败"

        audio_data = b""
        error_msg = None
        # 增加超时时间到60秒，确保长文本能被完整合成
        for _ in range(600):  # 等待60秒
            if not tts_queue.empty():
                type_, content = tts_queue.get()
                if type_ == "error":
                    error_msg = content
                    break
                elif type_ == "audio":
                    audio_data += content
                elif type_ == "done":
                    break
            time.sleep(0.1)
        
        # 如果指定了缓存ID，则保存音频文件
        if cache_id and audio_data:
            cache_path = os.path.join("static", f"tts_{cache_id}.wav")
            with wave.open(cache_path, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(16000)
                wf.writeframes(audio_data)
        
        return audio_data, error_msg
    finally:
        ws_client.close()


# 星火X1大模型
def spark_on_message(ws, message):
    try:
        data = json.loads(message)
        if data['header']['code'] != 0:
            spark_queue.put(("error", f"大模型错误: {data['header']['message']} (错误码: {data['header']['code']})"))
            return
        
        choices = data["payload"]["choices"]
        text_list = choices.get("text", [])
        for item in text_list:
            if "content" in item:
                spark_queue.put(("text", item["content"]))
        
        if choices["status"] == 2 or data["header"]["status"] == 2:
            spark_queue.put(("done", None))
    except Exception as e:
        spark_queue.put(("error", f"大模型解析错误: {str(e)}"))


def spark_chat(text_content):
    if not text_content:
        return "", "输入为空"

    class Spark_Ws_Param:
        def __init__(self):
            self.url = SPARK_URL
            self.host = urlparse(self.url).netloc
            self.path = urlparse(self.url).path
            self.APIKey = APIKey
            self.APISecret = APISecret

        def create_url(self):
            try:
                now = datetime.now()
                date = format_date_time(mktime(now.timetuple()))
                signature_origin = f"host: {self.host}\ndate: {date}\nGET {self.path} HTTP/1.1"
                signature_sha = hmac.new(
                    self.APISecret.encode('utf-8'),
                    signature_origin.encode('utf-8'),
                    digestmod=hashlib.sha256
                ).digest()
                signature = base64.b64encode(signature_sha).decode('utf-8')
                authorization_origin = (
                    f'api_key="{self.APIKey}", '
                    f'algorithm="hmac-sha256", '
                    f'headers="host date request-line", '
                    f'signature="{signature}"'
                )
                authorization = base64.b64encode(authorization_origin.encode('utf-8')).decode('utf-8')
                url_params = urlencode({
                    "authorization": authorization,
                    "date": date,
                    "host": self.host
                })
                return f"{self.url}?{url_params}"
            except Exception as e:
                return f"签名生成错误: {str(e)}"

    spark_param = Spark_Ws_Param()
    ws_url = spark_param.create_url()
    if "error" in ws_url:
        return "", ws_url

    ws_client = WebSocketClient(ws_url, spark_on_message)
    if not ws_client.connect():
        return "", "大模型连接失败"

    try:
        request_data = {
            "header": {
                "app_id": APPID,
                "uid": "user123"
            },
            "payload": {
                "message": {
                    "text": [
                        {"role": "user", "content": text_content}
                    ]
                }
            },
            "parameter": {
                "chat": {
                    "domain": SPARK_DOMAIN,
                    "max_tokens": 32768,
                    "temperature": 0.5,
                    "presence_penalty": 1,
                    "frequency_penalty": 0.02,
                    "top_k": 5,
                    "tools": [
                        {
                            "type": "web_search",
                            "web_search": {
                                "enable": False,
                                "search_mode": "normal"
                            }
                        }
                    ]
                }
            }
        }

        if not ws_client.send(json.dumps(request_data)):
            return "", "发送大模型请求失败"

        answer = ""
        error_msg = None
        for _ in range(600):
            if not spark_queue.empty():
                type_, content = spark_queue.get()
                if type_ == "error":
                    error_msg = content
                    break
                elif type_ == "text":
                    answer += content
                elif type_ == "done":
                    break
            time.sleep(0.1)
        
        return answer, error_msg
    finally:
        ws_client.close()


# 音频转换工具
def convert_audio_to_16k_mono_pcm(audio_bytes):
    try:
        data, samplerate = soundfile.read(audio_bytes)
        if len(data.shape) > 1:
            data = np.mean(data, axis=1)  # 转单声道
        if samplerate != 16000:
            data = resample(data, samplerate, 16000)  # 转16k采样率
        return (data * 32767).astype(np.int16).tobytes()  # 转16位PCM
    except Exception as e:
        print(f"音频转换错误: {e}")
        return b""


# 生成前端页面 - 增加自动朗读功能
with open("templates/index.html", "w", encoding="utf-8") as f:
    f.write('''<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>语音对话助手</title>
    <style>
        body { max-width: 800px; margin: 0 auto; padding: 20px; font-family: Arial, sans-serif; }
        #chat-history { height: 400px; border: 1px solid #ccc; border-radius: 5px; padding: 10px; overflow-y: auto; margin-bottom: 20px; }
        .message { margin: 10px 0; padding: 8px 12px; border-radius: 8px; max-width: 70%; }
        .user-message { background-color: #e3f2fd; margin-left: auto; }
        .bot-message { background-color: #f5f5f5; margin-right: auto; }
        .error-message { color: red; }
        #input-area { display: flex; gap: 10px; }
        #text-input { flex: 1; padding: 8px; border: 1px solid #ccc; border-radius: 5px; }
        button { padding: 8px 16px; background-color: #2196f3; color: white; border: none; border-radius: 5px; cursor: pointer; }
        button:hover { background-color: #0b7dda; }
        #audio-controls { margin-top: 10px; }
        .audio-player-container { margin: 5px 0 15px 0; }
        .controls { margin-top: 10px; display: flex; gap: 10px; align-items: center; }
        .tts-controls { margin-left: auto; }
    </style>
</head>
<body>
    <h1>语音对话助手</h1>
    <div id="chat-history"></div>
    <div class="controls">
        <div id="input-area">
            <input type="text" id="text-input" placeholder="输入文字...">
            <button onclick="sendText()">发送</button>
        </div>
        <div class="tts-controls">
            <label>
                <input type="checkbox" id="auto-read" checked> 自动朗读回复
            </label>
        </div>
    </div>
    <div id="audio-controls">
        <button onclick="startRecording()" id="record-btn">开始录音</button>
        <button onclick="stopRecording()" id="stop-btn" disabled>停止录音</button>
    </div>

    <script>
        let mediaRecorder;
        let audioChunks = [];
        let stream;
        let currentAudio = null;
        
        // 停止当前播放的音频
        function stopCurrentAudio() {
            if (currentAudio) {
                currentAudio.pause();
                currentAudio = null;
            }
        }
        
        function updateChatHistory(role, content, audioUrl = null, isError = false) {
            const historyDiv = document.getElementById('chat-history');
            const messageDiv = document.createElement('div');
            messageDiv.className = `message ${role}-message ${isError ? 'error-message' : ''}`;
            messageDiv.textContent = content;
            
            // 如果有音频URL，添加音频播放器
            if (audioUrl && role === 'bot') {
                const audioContainer = document.createElement('div');
                audioContainer.className = 'audio-player-container';
                
                const audioPlayer = document.createElement('audio');
                audioPlayer.controls = true;
                audioPlayer.src = audioUrl;
                audioPlayer.dataset.message = content;
                
                // 存储当前音频引用，用于停止播放
                audioPlayer.onplay = function() {
                    stopCurrentAudio();
                    currentAudio = this;
                };
                
                audioContainer.appendChild(audioPlayer);
                messageDiv.appendChild(audioContainer);
                
                // 如果自动朗读已开启，自动播放
                if (document.getElementById('auto-read').checked) {
                    setTimeout(() => {
                        stopCurrentAudio();
                        currentAudio = audioPlayer;
                        audioPlayer.play().catch(e => {
                            console.log('自动播放失败，可能需要用户交互:', e);
                        });
                    }, 500);
                }
            }
            
            historyDiv.appendChild(messageDiv);
            historyDiv.scrollTop = historyDiv.scrollHeight;
        }

        async function sendText() {
            const input = document.getElementById('text-input');
            const text = input.value.trim();
            if (!text) return;
            
            updateChatHistory('user', text);
            input.value = '';
            stopCurrentAudio();

            try {
                // 先获取聊天回复
                const chatResponse = await fetch('/chat', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
                    body: `text=${encodeURIComponent(text)}`
                });
                
                const chatData = await chatResponse.json();
                if (chatData.error) {
                    updateChatHistory('bot', `错误: ${chatData.error}`, null, true);
                    return;
                }
                
                // 再获取语音合成
                const ttsResponse = await fetch('/tts', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
                    body: `text=${encodeURIComponent(chatData.response || '')}`
                });
                
                if (ttsResponse.ok) {
                    const audioBlob = await ttsResponse.blob();
                    const audioUrl = URL.createObjectURL(audioBlob);
                    updateChatHistory('bot', chatData.response || '未获取到回复', audioUrl);
                } else {
                    updateChatHistory('bot', chatData.response || '未获取到回复');
                    updateChatHistory('bot', '语音合成失败', null, true);
                }
            } catch (error) {
                console.error('发送消息失败:', error);
                updateChatHistory('bot', '处理消息时出错，请重试', null, true);
            }
        }

        async function startRecording() {
            try {
                stream = await navigator.mediaDevices.getUserMedia({ audio: true });
                mediaRecorder = new MediaRecorder(stream);
                audioChunks = [];
                
                mediaRecorder.ondataavailable = (e) => {
                    audioChunks.push(e.data);
                };
                
                mediaRecorder.start();
                document.getElementById('record-btn').disabled = true;
                document.getElementById('stop-btn').disabled = false;
                updateChatHistory('bot', '正在录音...');
                stopCurrentAudio();
            } catch (error) {
                console.error('录音失败:', error);
                updateChatHistory('bot', '录音启动失败，请检查麦克风权限', null, true);
            }
        }

        async function stopRecording() {
            if (mediaRecorder && mediaRecorder.state !== 'inactive') {
                mediaRecorder.stop();
                stream.getTracks().forEach(track => track.stop());
                
                document.getElementById('record-btn').disabled = false;
                document.getElementById('stop-btn').disabled = true;
                
                mediaRecorder.onstop = async () => {
                    const audioBlob = new Blob(audioChunks, { type: 'audio/wav' });
                    const formData = new FormData();
                    formData.append('file', audioBlob, 'recording.wav');

                    try {
                        // 语音识别
                        const asrResponse = await fetch('/asr', {
                            method: 'POST',
                            body: formData
                        });
                        
                        const asrData = await asrResponse.json();
                        if (asrData.error) {
                            updateChatHistory('bot', `识别错误: ${asrData.error}`, null, true);
                            return;
                        }
                        updateChatHistory('user', asrData.text || '未识别到内容');

                        // 获取聊天回复
                        const chatResponse = await fetch('/chat', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
                            body: `text=${encodeURIComponent(asrData.text || '')}`
                        });
                        
                        const chatData = await chatResponse.json();
                        if (chatData.error) {
                            updateChatHistory('bot', `对话错误: ${chatData.error}`, null, true);
                            return;
                        }
                        
                        // 获取语音合成
                        const ttsResponse = await fetch('/tts', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
                            body: `text=${encodeURIComponent(chatData.response || '')}`
                        });
                        
                        if (ttsResponse.ok) {
                            const audioBlob = await ttsResponse.blob();
                            const audioUrl = URL.createObjectURL(audioBlob);
                            updateChatHistory('bot', chatData.response || '未获取到回复', audioUrl);
                        } else {
                            updateChatHistory('bot', chatData.response || '未获取到回复');
                            updateChatHistory('bot', '语音合成失败', null, true);
                        }
                    } catch (error) {
                        console.error('处理录音失败:', error);
                        updateChatHistory('bot', '处理录音时出错', null, true);
                    }
                };
            }
        }
    </script>
</body>
</html>''')


# API接口
@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.post("/asr")
async def handle_asr(file: UploadFile = File(...)):
    try:
        audio_bytes = await file.read()
        text, error = speech_to_text(audio_bytes)
        return JSONResponse({"text": text, "error": error})
    except Exception as e:
        return JSONResponse({"text": "", "error": f"ASR接口错误: {str(e)}"})


@app.post("/chat")
async def handle_chat(text: str = Form(...)):
    try:
        response, error = spark_chat(text)
        return JSONResponse({"response": response, "error": error})
    except Exception as e:
        return JSONResponse({"response": "", "error": f"Chat接口错误: {str(e)}"})


@app.post("/tts")
async def handle_tts(text: str = Form(...)):
    try:
        # 生成唯一ID用于缓存
        cache_id = hashlib.md5(text.encode()).hexdigest()[:10]
        audio_data, error = text_to_speech(text, cache_id)
        
        if error or not audio_data:
            return StreamingResponse(b"", media_type="audio/wav")
        
        # 直接从缓存文件读取并返回
        cache_path = os.path.join("static", f"tts_{cache_id}.wav")
        with open(cache_path, "rb") as f:
            return StreamingResponse(f, media_type="audio/wav")
    except Exception as e:
        print(f"TTS接口错误: {e}")
        return StreamingResponse(b"", media_type="audio/wav")


if __name__ == "__main__":
    os.makedirs("templates", exist_ok=True)
    os.makedirs("static", exist_ok=True)
    print("启动服务中...请访问 http://127.0.0.1:8000")
    uvicorn.run(app, host="127.0.0.1", port=8000)