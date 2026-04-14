# Sơ đồ khối hệ thống dạng luồng xử lý

```mermaid
flowchart TD
    A([Start]) --> B[Khai báo tham số hệ thống<br/>LoRa + WiFi + MQTT + Web]
    B --> C[Khởi tạo node cảm biến, gateway, web server]
    C --> D{Nhận gói dữ liệu từ node?}

    D -- "Không" --> D
    D -- "Có" --> E[Gateway xử lý gói<br/>tách node_id, nhiệt độ không khí, độ ẩm,<br/>nhiệt độ nước, TDS, validMask, RSSI, SNR]

    E --> F{Dữ liệu hợp lệ?}
    F -- "Không" --> D
    F -- "Có" --> G[Publish dữ liệu lên MQTT Broker]

    G --> H{Web backend nhận được MQTT?}
    H -- "Không" --> D
    H -- "Có" --> I[Lưu lịch sử RAM + CSV<br/>kèm cờ hợp lệ từng cảm biến]

    I --> J[Tính Capture theo node<br/>kỳ vọng, thực nhận, mất gói]
    J --> K[Cập nhật API /api/state<br/>bao gồm sensor_status + valid_mask]
    K --> L[Giao diện web vẽ chart + bảng capture<br/>invalid => hiển thị Mất dữ liệu]

    L --> M{Dừng hệ thống?}
    M -- "Không" --> D
    M -- "Có" --> N([Stop])
```
