import sys
import time
import serial

PORT = "/dev/ttyUSB0"
BAUD = 38400
PROMPT_STR = "CMD_PROMPT>"

def get_serial():
    ser = serial.Serial(PORT, BAUD, timeout=0.1)
    time.sleep(0.1)
    if ser.in_waiting:
        ser.read(ser.in_waiting)
    
    send_paced(ser, "\r\n")
    time.sleep(0.3)
    buf = ser.read(ser.in_waiting).decode('utf-8', errors='replace')
    
    if "login:" in buf.lower():
        send_paced(ser, "root\r\n")
        time.sleep(0.3)
        send_paced(ser, "YG*vd2!C2DL=LYXn3ni@\r\n")
        time.sleep(0.5)
        ser.read(ser.in_waiting)
        
    send_paced(ser, f'export PS1="{PROMPT_STR}> "\r\n')
    time.sleep(0.3)
    ser.read(ser.in_waiting)
    return ser

def send_paced(ser, text, chunk_size=16, delay=0.02):
    data = text.encode('utf-8') if isinstance(text, str) else text
    for i in range(0, len(data), chunk_size):
        ser.write(data[i:i+chunk_size])
        time.sleep(delay)

def run_cmd(ser, cmd, timeout=10):
    ser.reset_input_buffer()
    send_paced(ser, cmd.strip() + "\r\n")
    
    start = time.time()
    buf = ""
    while time.time() - start < timeout:
        if ser.in_waiting:
            chunk = ser.read(ser.in_waiting).decode('utf-8', errors='replace')
            buf += chunk
            lines = buf.replace('\r\n', '\n').replace('\r', '\n').split('\n')
            if len(lines) > 1 and any(PROMPT_STR in l for l in lines[1:]):
                break
        else:
            time.sleep(0.05)
            
    lines = buf.replace('\r\n', '\n').replace('\r', '\n').split('\n')
    filtered = []
    start_idx = 1 if len(lines) > 0 and cmd.strip() in lines[0] else 0
    for l in lines[start_idx:]:
        if PROMPT_STR not in l:
            filtered.append(l)
    return "\n".join(filtered).strip()

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 uart_exec.py '<command>' [timeout]")
        sys.exit(1)
    cmd = sys.argv[1]
    timeout = float(sys.argv[2]) if len(sys.argv) > 2 else 10
    ser = get_serial()
    res = run_cmd(ser, cmd, timeout=timeout)
    print(res)
    ser.close()
