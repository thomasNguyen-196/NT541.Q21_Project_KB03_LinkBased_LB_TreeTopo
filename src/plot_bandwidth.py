import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns
import matplotlib.dates as mdates

# Cài đặt giao diện chuẩn
sns.set_theme(style="whitegrid")

# Đọc dữ liệu
df = pd.read_csv('./res/bandwidth_stats.csv')
df['timestamp'] = pd.to_datetime(df['timestamp'], unit='s')

# Chuyển đổi sang Mbps và tạo nhãn link
df['Mbps'] = df['bps'] / 1_000_000
df['link'] = df['switch_id'].astype(str) + ':' + df['port'].astype(str)

# Định dạng thời gian chung cho trục X
time_fmt = mdates.DateFormatter('%H:%M:%S')

# 1. Biểu đồ đường tổng hợp
plt.figure(figsize=(14, 8))
sns.lineplot(data=df, x='timestamp', y='Mbps', hue='link', linewidth=2, marker='o', markersize=4)
plt.title('Bandwidth Usage Over Time Per Link', fontsize=16, pad=15)
plt.ylabel('Bandwidth (Mbps)', fontsize=12)
plt.xlabel('Time', fontsize=12)
plt.gca().xaxis.set_major_formatter(time_fmt)
plt.xticks(rotation=45)
plt.legend(title='Link (Switch:Port)', bbox_to_anchor=(1.01, 1), loc='upper left')
plt.tight_layout()
plt.savefig('bandwidth_time_series.png', dpi=300, bbox_inches='tight')
plt.close()

# 2. Biểu đồ riêng cho từng switch
switches = df['switch_id'].unique()
for sw in switches:
    df_sw = df[df['switch_id'] == sw]
    plt.figure(figsize=(12, 6))
    sns.lineplot(data=df_sw, x='timestamp', y='Mbps', hue='port', linewidth=2, marker='o', markersize=4)
    plt.title(f'Switch {sw} - Bandwidth Per Port', fontsize=14, pad=10)
    plt.ylabel('Bandwidth (Mbps)', fontsize=12)
    plt.xlabel('Time', fontsize=12)
    plt.gca().xaxis.set_major_formatter(time_fmt)
    plt.xticks(rotation=45)
    plt.legend(title='Port', bbox_to_anchor=(1.01, 1), loc='upper left')
    plt.tight_layout()
    plt.savefig(f'bandwidth_switch_{sw}.png', dpi=300, bbox_inches='tight')
    plt.close()

# 3. Thống kê tóm tắt
print("=== Thống kê băng thông (Mbps) ===")
summary = df.groupby('link')['Mbps'].describe()
print(summary)
