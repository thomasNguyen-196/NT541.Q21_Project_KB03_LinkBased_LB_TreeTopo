"""
         [ h_ext ]
             |
          [ s1 ]
         /      \
     [ s2 ]    [ s3 ]
     /    \    /    \
  [h1]  [h2]  [h3]  [h4]
"""
from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.link import TCLink
from mininet.cli import CLI
import time

def TreeTopo():
    # Khởi tạo mạng với Controller từ xa và hỗ trợ giới hạn băng thông (TCLink)
    net = Mininet(controller=RemoteController, link=TCLink, switch=OVSSwitch)

    print("*** Adding Controller - Hay dam bao Ryu dang chay o IP nay")
    # Thay 192.168.56.30 bang IP thuc te cua may Ryu
    net.addController('c0', controller=RemoteController, ip='192.168.56.30', port=6633)

    print("*** Adding Switches")
    s1 = net.addSwitch('s1', dpid='1', protocols='OpenFlow13')
    s2 = net.addSwitch('s2', dpid='2', protocols='OpenFlow13')
    s3 = net.addSwitch('s3', dpid='3', protocols='OpenFlow13')

    print("*** Adding Hosts")
    h1 = net.addHost('h1', ip='10.0.0.1/24', mac='00:00:00:00:00:01')
    h2 = net.addHost('h2', ip='10.0.0.2/24', mac='00:00:00:00:00:02')
    h3 = net.addHost('h3', ip='10.0.0.3/24', mac='00:00:00:00:00:03')
    h4 = net.addHost('h4', ip='10.0.0.4/24', mac='00:00:00:00:00:04')
    h_ext = net.addHost('h_ext', ip='10.0.0.5/24', mac='00:00:00:00:00:05')

    print("*** Creating Links - Moi duong chi co 10Mbps de de dang gay nghen")
    # h_ext noi vao s1
    net.addLink(h_ext, s1)
    
    net.addLink(s1, s2, bw=10) # Nhanh A
    net.addLink(s1, s3, bw=10) # Nhanh B

    # s2 noi ra 2 Server o nhanh A
    net.addLink(s2, h1, bw=10)
    net.addLink(s2, h2, bw=10)

    # s3 noi ra 2 Server o nhanh B
    net.addLink(s3, h3, bw=10)
    net.addLink(s3, h4, bw=10)

    print("*** Bat dau mang...")
    net.build()
    net.start()

    print("*** Tat cac tinh nang IPv6...")
    for host in net.hosts:
        host.cmd('sysctl -w net.ipv6.conf.all.disable_ipv6=1')
        host.cmd('sysctl -w net.ipv6.conf.default.disable_ipv6=1')
        # Xóa địa chỉ IPv6 có sẵn (nếu có)
        host.cmd('ip -6 addr flush dev lo')
        host.cmd('ip -6 addr flush dev %s' % host.defaultIntf())

    print('***Initializing iperf servers for h1-h4...')
    for host in [h1, h2, h3, h4]:
        host.cmd('iperf -s &')

    print("***Conducting ping test from h_ext...")
    print(net.hosts[4].cmd('ping -c 1 10.0.0.100'))

    print('***Conducting single iperf test from h_ext...')
    print(net.hosts[4].cmd('iperf -c 10.0.0.100 -t 10'))

    print('***Conducting multiple iperf tests (Forty-Eight 1Mbps parallel flows)...')
    for i in range (48):
        net.hosts[4].cmd('iperf -c 10.0.0.100 -b 1M -t 60 &')
        time.sleep(2)

    time.sleep(2)

    CLI(net)
    net.stop()

if __name__ == '__main__':
    TreeTopo()