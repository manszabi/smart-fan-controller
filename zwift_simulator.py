import socket
import time

# Zwift UDP beállítások
ZWIFT_HOST = "127.0.0.1"
ZWIFT_PORT = 3022

def create_zwift_udp_packet(power, cadence=85, heart_rate=140):
    """
    Egyszerű Zwift-szerű UDP csomag készítése.
    Protobuf-szerű struktúra (field 4 = power).
    """
    # Protobuf header (4 byte mock)
    header = b'\x00\x00\x00\x00'
    
    # Field 1: id (varint)
    field1 = b'\x08\x01'
    
    # Field 2: world_time (varint)
    field2 = b'\x10\xAA\xBB\x01'
    
    # Field 3: timestamp (varint)
    field3 = b'\x18\xCC\xDD\xEE\xFF\x01'
    
    # Field 4: POWER (varint) ← EZ A FONTOS!
    tag4 = 0x20  # field_number=4, wire_type=0 (varint)
    power_bytes = encode_varint(power)
    field4 = bytes([tag4]) + power_bytes
    
    # Field 5: cadence (varint)
    tag5 = 0x28  # field_number=5, wire_type=0
    cadence_bytes = encode_varint(cadence)
    field5 = bytes([tag5]) + cadence_bytes
    
    # Field 6: heart_rate (varint)
    tag6 = 0x30  # field_number=6, wire_type=0
    hr_bytes = encode_varint(heart_rate)
    field6 = bytes([tag6]) + hr_bytes
    
    # Összefűzés
    packet = header + field1 + field2 + field3 + field4 + field5 + field6
    return packet

def encode_varint(value):
    """Protobuf varint kódolás"""
    result = []
    while value > 0x7F:
        result.append((value & 0x7F) | 0x80)
        value >>= 7
    result.append(value & 0x7F)
    return bytes(result)

def simulate_ride():
    """
    Kerékpározás szimulációja:
    - Bemelegítés: 100W → 150W
    - Kemény szakasz: 200W → 300W
    - Könnyítés: 150W
    - Sprint: 400W!
    - Lehűlés: 100W → 0W
    """
    
    print("=" * 60)
    print("  Zwift Szimulátor - UDP teljesítmény küldés")
    print("=" * 60)
    print(f"Cél: {ZWIFT_HOST}:{ZWIFT_PORT}")
    print()
    
    # UDP socket létrehozása
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    
    try:
        # 1. Bemelegítés (15s: 100W → 150W)
        print("🚴 1. Bemelegítés (15s: 100W → 150W)")
        for i in range(15):
            power = 100 + (i * 3)
            cadence = 70 + i
            packet = create_zwift_udp_packet(power, cadence)
            sock.sendto(packet, (ZWIFT_HOST, ZWIFT_PORT))
            print(f"  ⏱ {i+1:2d}s | 💪 {power:3d}W | 🔄 {cadence:2d} rpm")
            time.sleep(1)
        
        print()
        
        # 2. Kemény szakasz (20s: 200W → 300W)
        print("🔥 2. Kemény szakasz (20s: 200W → 300W)")
        for i in range(20):
            power = 200 + (i * 5)
            cadence = 85 + (i // 2)
            packet = create_zwift_udp_packet(power, cadence)
            sock.sendto(packet, (ZWIFT_HOST, ZWIFT_PORT))
            print(f"  ⏱ {i+1:2d}s | 💪 {power:3d}W | 🔄 {cadence:2d} rpm")
            time.sleep(1)
        
        print()
        
        # 3. Könnyítés (10s: 150W)
        print("😌 3. Könnyítés (10s: 150W)")
        for i in range(10):
            power = 150
            cadence = 75
            packet = create_zwift_udp_packet(power, cadence)
            sock.sendto(packet, (ZWIFT_HOST, ZWIFT_PORT))
            print(f"  ⏱ {i+1:2d}s | 💪 {power:3d}W | 🔄 {cadence:2d} rpm")
            time.sleep(1)
        
        print()
        
        # 4. SPRINT! (5s: 400W)
        print("⚡ 4. SPRINT! (5s: 400W)")
        for i in range(5):
            power = 400
            cadence = 110
            packet = create_zwift_udp_packet(power, cadence)
            sock.sendto(packet, (ZWIFT_HOST, ZWIFT_PORT))
            print(f"  ⏱ {i+1:2d}s | 💪 {power:3d}W | 🔄 {cadence:2d} rpm")
            time.sleep(1)
        
        print()
        
        # 5. Lehűlés (15s: 100W → 0W)
        print("❄️  5. Lehűlés (15s: 100W → 0W)")
        for i in range(15):
            power = 100 - (i * 7)
            if power < 0:
                power = 0
            cadence = 70 - (i * 4)
            if cadence < 0:
                cadence = 0
            packet = create_zwift_udp_packet(power, cadence)
            sock.sendto(packet, (ZWIFT_HOST, ZWIFT_PORT))
            print(f"  ⏱ {i+1:2d}s | 💪 {power:3d}W | 🔄 {cadence:2d} rpm")
            time.sleep(1)
        
        print()
        print("✅ Szimuláció befejezve!")
        print()
        
    except KeyboardInterrupt:
        print("\n\n⏹  Szimuláció megszakítva")
    finally:
        sock.close()
        print("✓ Socket bezárva")

if __name__ == "__main__":
    simulate_ride()