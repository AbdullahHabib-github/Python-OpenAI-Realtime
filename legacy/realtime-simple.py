import os
os.system('cls' if os.name == 'nt' else 'clear')

import threading
import pyaudio
import queue
import base64
import json
import time
from websocket import create_connection, WebSocketConnectionClosedException
from dotenv import load_dotenv
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

load_dotenv()

response_audio_buffers = []
current_response_buffer = bytearray()
response_counter = 0


CHUNK_SIZE = 1024
RATE = 24000
FORMAT = pyaudio.paInt16
API_KEY = os.getenv('OPENAI_API_KEY')
WS_URL = 'wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview-2024-12-17'
prompt = '''
Introduce yourself as Socrates. Socrates is a calm, friendly, and supportive real-time AI assistant designed to help adult students with ADHD understand academic concepts clearly.

Always explain ideas in small, manageable chunks. Use simple, direct language and speak in a calm, positive tone. Avoid overwhelming the student with too much information at once.

Frequently offer to pause, summarize, or repeat key points. If the student seems confused, rephrase kindly and patiently without judgment.

Always respond in the same language the student uses. The conversation will either be in English or Urdu. Match the student's language naturally and respectfully.

Use positive reinforcement to encourage the student's efforts. Invite questions warmly, and reassure them that taking things step-by-step is perfectly okay.

Focus on clarity, empathy, and encouragement. Break down complex ideas into simple parts, and use real-world examples when helpful.

Your goal is to make learning feel achievable, supportive, and stress-free, helping the student stay engaged, confident, and understood at all times.'''


audio_buffer = bytearray()
mic_queue = queue.Queue()

stop_event = threading.Event()

mic_on_at = 0
mic_active = None
REENGAGE_DELAY_MS = 500

import wave

def save_response_audio(audio_data):
    global response_counter

    filename = f'response_{response_counter}.wav'
    response_counter += 1

    with wave.open(filename, 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # 16-bit audio = 2 bytes
        wf.setframerate(RATE)
        wf.writeframes(audio_data)

    logging.info(f'💾 Saved {filename}')


def mic_callback(in_data, frame_count, time_info, status):
    global mic_on_at, mic_active

    if time.time() > mic_on_at:
        if mic_active != True:
            logging.info('🎙️🟢 Mic active')
            mic_active = True
        mic_queue.put(in_data)
    else:
        if mic_active != False:
            logging.info('🎙️🔴 Mic suppressed')
            mic_active = False

    return (None, pyaudio.paContinue)


def send_mic_audio_to_websocket(ws):
    try:
        while not stop_event.is_set():
            if not mic_queue.empty():
                mic_chunk = mic_queue.get()
                logging.info(f'🎤 Sending {len(mic_chunk)} bytes of audio data.')
                encoded_chunk = base64.b64encode(mic_chunk).decode('utf-8')
                message = json.dumps({'type': 'input_audio_buffer.append', 'audio': encoded_chunk})
                try:
                    ws.send(message)
                except WebSocketConnectionClosedException:
                    logging.error('WebSocket connection closed.')
                    break
                except Exception as e:
                    logging.error(f'Error sending mic audio: {e}')
    except Exception as e:
        logging.error(f'Exception in send_mic_audio_to_websocket thread: {e}')
    finally:
        logging.info('Exiting send_mic_audio_to_websocket thread.')


def spkr_callback(in_data, frame_count, time_info, status):
    global audio_buffer, mic_on_at

    bytes_needed = frame_count * 2
    current_buffer_size = len(audio_buffer)

    if current_buffer_size >= bytes_needed:
        audio_chunk = bytes(audio_buffer[:bytes_needed])
        audio_buffer = audio_buffer[bytes_needed:]
        mic_on_at = time.time() + REENGAGE_DELAY_MS / 1000
    else:
        audio_chunk = bytes(audio_buffer) + b'\x00' * (bytes_needed - current_buffer_size)
        audio_buffer.clear()

    return (audio_chunk, pyaudio.paContinue)


def receive_audio_from_websocket(ws):
    global audio_buffer

    try:
        while not stop_event.is_set():
            try:
                message = ws.recv()
                if not message:  # Handle empty message (EOF or connection close)
                    logging.info('🔵 Received empty message (possibly EOF or WebSocket closing).')
                    break

                # Now handle valid JSON messages only
                message = json.loads(message)
                event_type = message['type']
                logging.info(f'⚡️ Received WebSocket event: {event_type}')

                if event_type == 'response.audio.delta':
                    audio_content = base64.b64decode(message['delta'])
                    audio_buffer.extend(audio_content)
                    current_response_buffer.extend(audio_content)
                    logging.info(f'🔵 Received {len(audio_content)} bytes, total buffer size: {len(audio_buffer)}')

                elif event_type == 'response.audio.done':
                    logging.info('🔵 AI finished speaking.')

                    # Save the current_response_buffer
                    response_audio_buffers.append(current_response_buffer)
                    save_response_audio(current_response_buffer)
                    current_response_buffer = bytearray()  # reset for next response


            except WebSocketConnectionClosedException:
                logging.error('WebSocket connection closed.')
                break
            except Exception as e:
                logging.error(f'Error receiving audio: {e}')
    except Exception as e:
        logging.error(f'Exception in receive_audio_from_websocket thread: {e}')
    finally:
        logging.info('Exiting receive_audio_from_websocket thread.')


def connect_to_openai():
    ws = None
    try:
        ws = create_connection(WS_URL, header=[f'Authorization: Bearer {API_KEY}', 'OpenAI-Beta: realtime=v1'])
        logging.info('Connected to OpenAI WebSocket.')

        ws.send(json.dumps({
            'type': 'response.create',
            'response': {
                'modalities': ['audio', 'text'],
                'instructions': prompt,
            }
        }))

        # Start the recv and send threads
        receive_thread = threading.Thread(target=receive_audio_from_websocket, args=(ws,))
        receive_thread.start()

        mic_thread = threading.Thread(target=send_mic_audio_to_websocket, args=(ws,))
        mic_thread.start()

        # Wait for stop_event to be set
        while not stop_event.is_set():
            time.sleep(0.1)

        # Send a close frame and close the WebSocket gracefully
        logging.info('Sending WebSocket close frame.')
        ws.send_close()

        receive_thread.join()
        mic_thread.join()

        logging.info('WebSocket closed and threads terminated.')
    except Exception as e:
        logging.error(f'Failed to connect to OpenAI: {e}')
    finally:
        if ws is not None:
            try:
                ws.close()
                logging.info('WebSocket connection closed.')
            except Exception as e:
                logging.error(f'Error closing WebSocket connection: {e}')


def main():
    p = pyaudio.PyAudio()

    mic_stream = p.open(
        format=FORMAT,
        channels=1,
        rate=RATE,
        input=True,
        stream_callback=mic_callback,
        frames_per_buffer=CHUNK_SIZE
    )

    spkr_stream = p.open(
        format=FORMAT,
        channels=1,
        rate=RATE,
        output=True,
        stream_callback=spkr_callback,
        frames_per_buffer=CHUNK_SIZE
    )

    try:
        mic_stream.start_stream()
        spkr_stream.start_stream()

        connect_to_openai()

        while mic_stream.is_active() and spkr_stream.is_active():
            time.sleep(0.1)

    except KeyboardInterrupt:
        logging.info('Gracefully shutting down...')
        stop_event.set()

    finally:
        mic_stream.stop_stream()
        mic_stream.close()
        spkr_stream.stop_stream()
        spkr_stream.close()

        p.terminate()
        logging.info('Audio streams stopped and resources released. Exiting.')


if __name__ == '__main__':
    main()