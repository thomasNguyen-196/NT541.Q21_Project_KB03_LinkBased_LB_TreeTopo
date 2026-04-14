from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet, ether_types, arp, ipv4
from ryu.lib import hub
from ryu.lib.packet import tcp, udp
import csv
import time
import atexit

# Constants
S1_DPID, S2_DPID, S3_DPID = 1, 2, 3
VIP = '10.0.0.100'
VMAC = '00:00:00:00:00:FE'
H_EXT_MAC = '00:00:00:00:00:05'

SERVERS = {
    'h1': {'ip': '10.0.0.1', 'mac': '00:00:00:00:00:01', 's1_port': 2, 's_dpid': S2_DPID, 's_port': 2},
    'h2': {'ip': '10.0.0.2', 'mac': '00:00:00:00:00:02', 's1_port': 2, 's_dpid': S2_DPID, 's_port': 3},
    'h3': {'ip': '10.0.0.3', 'mac': '00:00:00:00:00:03', 's1_port': 3, 's_dpid': S3_DPID, 's_port': 2},
    'h4': {'ip': '10.0.0.4', 'mac': '00:00:00:00:00:04', 's1_port': 3, 's_dpid': S3_DPID, 's_port': 3},
}

MONITOR_INTERVAL = 3
FLOW_IDLE_TIMEOUT = 60

class DynamicCascadingLB(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.datapaths = {}
        
        # Thống kê băng thông (bps)
        self.port_stats = {
            S1_DPID: {
                2: {'prev_tx': 0, 'prev_rx': 0, 'bps': 0.0},
                3: {'prev_tx': 0, 'prev_rx': 0, 'bps': 0.0}
            },
            S2_DPID: {
                2: {'prev_tx': 0, 'prev_rx': 0, 'bps': 0.0},
                3: {'prev_tx': 0, 'prev_rx': 0, 'bps': 0.0}
            },
            S3_DPID: {
                2: {'prev_tx': 0, 'prev_rx': 0, 'bps': 0.0},
                3: {'prev_tx': 0, 'prev_rx': 0, 'bps': 0.0}
            },
        }
        
        self.monitor_thread = hub.spawn(self._monitor_loop)

        # Mở file CSV để ghi số liệu
        self.stats_file = open('bandwidth_stats.csv', 'w', newline='')
        self.csv_writer = csv.writer(self.stats_file)
        self.csv_writer.writerow(['timestamp', 'switch_id', 'port', 'bps'])
        atexit.register(self.close)

    def close(self):
        self.stats_file.close()

    def _monitor_loop(self):
        while True:
            hub.sleep(MONITOR_INTERVAL)
            for dp in self.datapaths.values():
                self._request_port_stats(dp)

    def _request_port_stats(self, datapath):
        parser = datapath.ofproto_parser
        req = parser.OFPPortStatsRequest(datapath, 0, datapath.ofproto.OFPP_ANY)
        datapath.send_msg(req)

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def _port_stats_reply_handler(self, ev):
        dpid = ev.msg.datapath.id
        if dpid not in self.port_stats: return

        for stat in ev.msg.body:
            port = stat.port_no
            if port in self.port_stats[dpid]:
                ps = self.port_stats[dpid][port]
                delta_tx = stat.tx_bytes - ps['prev_tx']
                delta_rx = stat.rx_bytes - ps['prev_rx']
                delta_total = delta_tx + delta_rx
                ps['bps'] = (delta_total * 8) / float(MONITOR_INTERVAL) if ps['prev_tx'] > 0 or ps['prev_rx'] > 0 else 0.0
                ps['prev_tx'] = stat.tx_bytes
                ps['prev_rx'] = stat.rx_bytes
                
                # Ghi vào file CSV
                self.csv_writer.writerow([time.time(), dpid, port, ps['bps']])
                self.stats_file.flush()  # ghi ngay lập tức

        # Log trạng thái định kỳ cho cả 3 switch
        if dpid == S1_DPID:
            self.logger.info("[S1] s2: %.2f bps | s3: %.2f bps", 
                            self.port_stats[1][2]['bps'], self.port_stats[1][3]['bps'])
        elif dpid == S2_DPID:
            self.logger.info("[S2] h1: %.2f bps | h2: %.2f bps", 
                            self.port_stats[2][2]['bps'], self.port_stats[2][3]['bps'])
        elif dpid == S3_DPID:
            self.logger.info("[S3] h3: %.2f bps | h4: %.2f bps", 
                            self.port_stats[3][2]['bps'], self.port_stats[3][3]['bps'])

    def _select_server(self):
        """Tính toán định tuyến phân tầng dựa trên BPS với ngưỡng 80% (8 Mbps)"""
        THRESHOLD = 8_000_000  # 80% của 10 Mbps

        # Tầng 1: Chọn nhánh phân phối tại s1
        bps_to_s2 = self.port_stats[S1_DPID][2]['bps']
        bps_to_s3 = self.port_stats[S1_DPID][3]['bps']

        # Fallback round-robin nếu chưa có dữ liệu BPS
        all_zero = all(
            self.port_stats[dpid][port]['bps'] == 0.0
            for dpid in [S1_DPID, S2_DPID, S3_DPID]
            for port in [2, 3]
        )
        if all_zero:
            self._rr_index = (getattr(self, '_rr_index', -1) + 1) % 4
            selected = list(SERVERS.values())[self._rr_index]
            self.logger.info("All BPS zero, using round-robin: %s", selected['ip'])
            return selected

        # Hàm chọn giữa hai lựa chọn (a hoặc b) dựa trên bps và ngưỡng
        def choose(a_bps, b_bps):
            # Nếu một bên dưới ngưỡng và bên kia trên ngưỡng, chọn bên dưới
            if a_bps < THRESHOLD and b_bps >= THRESHOLD:
                return 'a'
            if b_bps < THRESHOLD and a_bps >= THRESHOLD:
                return 'b'
            # Cả hai cùng dưới hoặc cùng trên ngưỡng -> chọn bên có tải thấp hơn
            return 'a' if a_bps <= b_bps else 'b'

        # Chọn nhánh
        branch = choose(bps_to_s2, bps_to_s3)
        self.logger.info("Branch selection: bps to s2=%.2f, s3=%.2f -> choose %s",
                        bps_to_s2, bps_to_s3, 's2' if branch == 'a' else 's3')

        if branch == 'a':  # chọn nhánh s2
            bps_h1 = self.port_stats[S2_DPID][2]['bps']
            bps_h2 = self.port_stats[S2_DPID][3]['bps']
            server_choice = choose(bps_h1, bps_h2)
            selected = SERVERS['h1'] if server_choice == 'a' else SERVERS['h2']
        else:  # chọn nhánh s3
            bps_h3 = self.port_stats[S3_DPID][2]['bps']
            bps_h4 = self.port_stats[S3_DPID][3]['bps']
            server_choice = choose(bps_h3, bps_h4)
            selected = SERVERS['h3'] if server_choice == 'a' else SERVERS['h4']

        self.logger.info("Selected server: %s (%s)", selected['ip'], selected['mac'])
        return selected

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def _switch_features_handler(self, ev):
        datapath = ev.msg.datapath
        self.datapaths[datapath.id] = datapath
        self.logger.info("Switch %d connected", datapath.id)
        parser = datapath.ofproto_parser

        if datapath.id == S1_DPID:
            match = parser.OFPMatch()
            actions = [parser.OFPActionOutput(datapath.ofproto.OFPP_CONTROLLER, datapath.ofproto.OFPCML_NO_BUFFER)]
            self._add_flow(datapath, 0, match, actions)
            # Định tuyến tĩnh cho lưu lượng phản hồi về h_ext
            self._add_flow(datapath, 10, parser.OFPMatch(eth_dst=H_EXT_MAC), [parser.OFPActionOutput(1)])
        else:
            # s2 và s3 được cấu hình L2 tĩnh
            self._install_static_l2_flows(datapath)

    def _install_static_l2_flows(self, datapath):
        dpid = datapath.id
        self.logger.info("Installing static L2 flows on switch %d", dpid)

        parser = datapath.ofproto_parser
        
        self._add_flow(datapath, 10, parser.OFPMatch(eth_dst='ff:ff:ff:ff:ff:ff'), [parser.OFPActionOutput(datapath.ofproto.OFPP_FLOOD)])
        self._add_flow(datapath, 20, parser.OFPMatch(eth_dst=H_EXT_MAC), [parser.OFPActionOutput(1)])
        
        self.logger.info("Added flow on s%d: eth_dst=ff:ff:ff:ff:ff:ff -> FLOOD", dpid)
        self.logger.info("Added flow on s%d: eth_dst=%s -> port 1", dpid, H_EXT_MAC)
        for srv in SERVERS.values():
            if srv['s_dpid'] == dpid:
                self._add_flow(datapath, 20, parser.OFPMatch(eth_dst=srv['mac']), [parser.OFPActionOutput(srv['s_port'])])
                self.logger.info("Added flow on s%d: eth_dst=%s -> port %d", dpid, srv['mac'], srv['s_port'])

    def _add_flow(self, datapath, priority, match, actions, idle_timeout=0):
        parser = datapath.ofproto_parser
        inst = [parser.OFPInstructionActions(datapath.ofproto.OFPIT_APPLY_ACTIONS, actions)]
        mod = parser.OFPFlowMod(datapath=datapath, priority=priority, match=match, instructions=inst, idle_timeout=idle_timeout)
        datapath.send_msg(mod)

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in_handler(self, ev):
        msg = ev.msg
        datapath = msg.datapath
        if datapath.id != S1_DPID: return

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocols(ethernet.ethernet)[0]

        self.logger.info("[PKT_IN] ethertype=0x%04x src=%s dst=%s", 
                        eth.ethertype, eth.src, eth.dst)
        if eth.ethertype in (ether_types.ETH_TYPE_LLDP, ether_types.ETH_TYPE_IPV6): return

        in_port = msg.match['in_port']
        arp_pkt = pkt.get_protocol(arp.arp)
        
        if arp_pkt and arp_pkt.opcode == arp.ARP_REQUEST and arp_pkt.dst_ip == VIP:
            self._reply_arp(datapath, in_port, eth, arp_pkt)
            return

        ipv4_pkt = pkt.get_protocol(ipv4.ipv4)
        if ipv4_pkt and ipv4_pkt.dst == VIP:
            # Parse transport layer
            tcp_pkt = pkt.get_protocol(tcp.tcp)
            udp_pkt = pkt.get_protocol(udp.udp)
            if tcp_pkt:
                src_port = tcp_pkt.src_port
                dst_port = tcp_pkt.dst_port
                proto = 6
            elif udp_pkt:
                src_port = udp_pkt.src_port
                dst_port = udp_pkt.dst_port
                proto = 17
            else:
                # Non-TCP/UDP (ICMP, etc.) – có thể xử lý riêng hoặc bỏ qua
                src_port = 0
                dst_port = 0
                proto = 1  # ICMP
            
            self.logger.info("PacketIn: IPv4 to VIP from %s (src_mac=%s)", ipv4_pkt.src, eth.src)
            server = self._select_server()
            self.logger.info("Selected server: %s (%s) via s1 port %d", server['ip'], server['mac'], server['s1_port'])
            
            self._install_nat_flows(datapath, ipv4_pkt.src, server, src_port, dst_port, proto)
            
            parser = datapath.ofproto_parser
            actions = [
                parser.OFPActionSetField(eth_dst=server['mac']),
                parser.OFPActionSetField(ipv4_dst=server['ip']),
                parser.OFPActionOutput(server['s1_port'])
            ]
            out = parser.OFPPacketOut(datapath=datapath, buffer_id=msg.buffer_id, in_port=in_port, actions=actions, data=msg.data)
            datapath.send_msg(out)

        if arp_pkt and arp_pkt.opcode == arp.ARP_REQUEST:
            # Kiểm tra xem địa chỉ đích có phải là IP của client không (10.0.0.5)
            if arp_pkt.dst_ip == '10.0.0.5':   # hoặc lấy từ hằng số H_EXT_IP nếu có
                self.logger.info("ARP request from %s for client %s, replying with MAC %s", 
                                arp_pkt.src_ip, arp_pkt.dst_ip, H_EXT_MAC)
                self._reply_arp_client(datapath, in_port, eth, arp_pkt)
                return

    def _reply_arp(self, datapath, in_port, eth_pkt, arp_pkt):
        self.logger.info("ARP request for %s from %s, sending reply", VIP, arp_pkt.src_ip)
        parser = datapath.ofproto_parser
        pkt_out = packet.Packet()
        pkt_out.add_protocol(ethernet.ethernet(ethertype=ether_types.ETH_TYPE_ARP, dst=eth_pkt.src, src=VMAC))
        pkt_out.add_protocol(arp.arp(opcode=arp.ARP_REPLY, src_mac=VMAC, src_ip=VIP, dst_mac=arp_pkt.src_mac, dst_ip=arp_pkt.src_ip))
        pkt_out.serialize()
        actions = [parser.OFPActionOutput(in_port)]
        datapath.send_msg(parser.OFPPacketOut(datapath=datapath, buffer_id=datapath.ofproto.OFP_NO_BUFFER, in_port=datapath.ofproto.OFPP_CONTROLLER, actions=actions, data=pkt_out.data))

    def _reply_arp_client(self, datapath, in_port, eth_pkt, arp_pkt):
        parser = datapath.ofproto_parser
        pkt_out = packet.Packet()
        pkt_out.add_protocol(ethernet.ethernet(ethertype=ether_types.ETH_TYPE_ARP, dst=eth_pkt.src, src=H_EXT_MAC))
        pkt_out.add_protocol(arp.arp(opcode=arp.ARP_REPLY, src_mac=H_EXT_MAC, src_ip=arp_pkt.dst_ip, 
                                    dst_mac=arp_pkt.src_mac, dst_ip=arp_pkt.src_ip))
        pkt_out.serialize()
        actions = [parser.OFPActionOutput(in_port)]
        datapath.send_msg(parser.OFPPacketOut(datapath=datapath, buffer_id=datapath.ofproto.OFP_NO_BUFFER,
                                            in_port=datapath.ofproto.OFPP_CONTROLLER, 
                                            actions=actions, data=pkt_out.data))

    def _install_nat_flows(self, datapath, client_ip, server, client_port, vip_port, proto):
        self.logger.info("Installing NAT flows for client %s:%d -> VIP:%d proto=%d to server %s",
                        client_ip, client_port, vip_port, proto, server['ip'])
        parser = datapath.ofproto_parser

        # Tạo match forward (client -> VIP)
        match_fwd = parser.OFPMatch(
            in_port=1,
            eth_type=ether_types.ETH_TYPE_IP,
            ip_proto=proto,
            ipv4_src=client_ip,
            ipv4_dst=VIP,
        )
        # Thêm match cho port nếu là TCP/UDP
        if proto == 6:
            match_fwd = parser.OFPMatch(
                in_port=1, eth_type=ether_types.ETH_TYPE_IP, ip_proto=proto,
                ipv4_src=client_ip, ipv4_dst=VIP,
                tcp_src=client_port, tcp_dst=vip_port
            )
        elif proto == 17:
            match_fwd = parser.OFPMatch(
                in_port=1, eth_type=ether_types.ETH_TYPE_IP, ip_proto=proto,
                ipv4_src=client_ip, ipv4_dst=VIP,
                udp_src=client_port, udp_dst=vip_port
            )
        # Nếu là ICMP (proto=1) thì không có port, giữ match cơ bản

        actions_fwd = [
            parser.OFPActionSetField(eth_dst=server['mac']),
            parser.OFPActionSetField(ipv4_dst=server['ip']),
            parser.OFPActionOutput(server['s1_port'])
        ]
        self._add_flow(datapath, 20, match_fwd, actions_fwd, idle_timeout=FLOW_IDLE_TIMEOUT)
        self.logger.info("Forward flow: in_port=1, %s:%d -> VIP:%d (proto=%d) -> output %d",
                        client_ip, client_port, vip_port, proto, server['s1_port'])

        # Tạo match reverse (server -> client)
        match_rev = parser.OFPMatch(
            in_port=server['s1_port'],
            eth_type=ether_types.ETH_TYPE_IP,
            ip_proto=proto,
            ipv4_src=server['ip'],
            ipv4_dst=client_ip,
        )
        if proto == 6:
            match_rev = parser.OFPMatch(
                in_port=server['s1_port'], eth_type=ether_types.ETH_TYPE_IP, ip_proto=proto,
                ipv4_src=server['ip'], ipv4_dst=client_ip,
                tcp_src=vip_port, tcp_dst=client_port   # Server gửi với src_port = vip_port
            )
        elif proto == 17:
            match_rev = parser.OFPMatch(
                in_port=server['s1_port'], eth_type=ether_types.ETH_TYPE_IP, ip_proto=proto,
                ipv4_src=server['ip'], ipv4_dst=client_ip,
                udp_src=vip_port, udp_dst=client_port
            )

        actions_rev = [
            parser.OFPActionSetField(eth_src=VMAC),
            parser.OFPActionSetField(ipv4_src=VIP),
            parser.OFPActionOutput(1)
        ]
        self._add_flow(datapath, 20, match_rev, actions_rev, idle_timeout=FLOW_IDLE_TIMEOUT)
        self.logger.info("Reverse flow: in_port=%d, %s:%d -> %s:%d (proto=%d) -> output 1",
                        server['s1_port'], server['ip'], vip_port, client_ip, client_port, proto)
