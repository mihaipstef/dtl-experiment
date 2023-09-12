from gnuradio import (analog,
                      blocks,
                      channels,
                      dtl,
                      gr,
                      network,
                      pdu,)


class _ofdm_adaptive_sim(gr.top_block):

    def __new__(cls, *args, **kwargs):
        # Incomplete - don't allow instantiation
        if cls is _ofdm_adaptive_sim:
            raise TypeError(
                f"only children of '{cls.__name__}' may be instantiated")
        return object.__new__(cls)

    def __init__(self, config_dict, run_config_file):
        gr.top_block.__init__(
            self, "OFDM Adaptive Simulator", catch_exceptions=True)
        ofdm_config = config_dict.get("ofdm_config", {})
        self.samp_rate = samp_rate = config_dict.get("sample_rate", 200000)
        self.n_bytes = 100
        self.direct_channel_noise_level = 0.0001
        self.direct_channel_freq_offset = 0.5
        self.fft_len = 64
        self.cp_len = 16
        self.run_config_file = run_config_file
        self.use_sync_correct = ofdm_config.get("use_sync_correct", True)
        self.max_doppler = 0
        self.propagation_paths = config_dict.get(
            "propagation_paths", [(0, 0, 0, 1)])
        self.frame_length = ofdm_config.get("frame_length", 20)
        self.frame_samples = (self.frame_length + 4) * \
            (self.fft_len + self.cp_len)
        self.data_bytes = config_dict.get("data_bytes", None)

        ##################################################
        # Blocks
        ##################################################

        #self.zeromq_pub = zeromq.pub_msg_sink('tcp://0.0.0.0:5552', 100, True)
        self.tx = dtl.ofdm_adaptive_tx.from_parameters(
            config_dict=ofdm_config,
            fft_len=self.fft_len,
            cp_len=self.cp_len,
            rolloff=0,
            scramble_bits=False,
            frame_length=self.frame_length,
        )
        self.rx = dtl.ofdm_adaptive_rx.from_parameters(
            config_dict=ofdm_config,
            fft_len=self.fft_len,
            cp_len=self.cp_len,
            rolloff=0,
            scramble_bits=False,
            use_sync_correct=self.use_sync_correct,
            frame_length=self.frame_length,
        )
        delays, delays_std, delays_maxdev, mags = zip(*self.propagation_paths)
        self.fadding_channel = channels.selective_fading_model2(
            8, self.max_doppler, False, 4.0, 0, delays, delays_std, delays_maxdev, mags, 8)
        self.awgn_channel = channels.channel_model(
            noise_voltage=0.0,
            frequency_offset=0.0,
            epsilon=1.0,
            taps=[1.0 + 1.0j],
            noise_seed=0,
            block_tags=True)
        self.throtle = blocks.throttle(gr.sizeof_gr_complex*1, samp_rate, True)

        self.msg_debug = blocks.message_debug(True)
        print(config_dict)
        monitor_address = config_dict.get(
            "monitor_probe", "tcp://127.0.0.1:5555")
        monitor_probe_name = config_dict.get("monitor_probe_name", "probe")

        self.monitor_probe = dtl.zmq_probe(
            monitor_address, monitor_probe_name, bind=True)

    def get_samp_rate(self):
        return self.samp_rate

    def set_samp_rate(self, samp_rate):
        self.samp_rate = samp_rate
        self.throtle.set_sample_rate(self.samp_rate)
        self.fadding_channel.set_fDTs((0/self.samp_rate))

    def get_n_bytes(self):
        return self.n_bytes

    def set_n_bytes(self, n_bytes):
        self.n_bytes = n_bytes

    def set_direct_channel_noise_level(self, direct_channel_noise_level):
        self.direct_channel_noise_level = float(direct_channel_noise_level)
        self.awgn_channel.set_noise_voltage(self.direct_channel_noise_level)

    def set_direct_channel_freq_offset(self, direct_channel_freq_offset):
        self.direct_channel_freq_offset = direct_channel_freq_offset
        self.awgn_channel.set_frequency_offset(self.direct_channel_freq_offset)

    def set_max_doppler(self, val):
        self.max_doppler = val
        self.fadding_channel.set_fDTs(self.max_doppler)


    def wire_it(self):

        # Direct path
        self.connect(
            (self.tx, 0),
            (self.throtle, 0),
            (self.fadding_channel, 0),
            (self.awgn_channel, 0),
            (self.rx, 0)
        )
        # Feedback path
        self.connect(
            (self.rx, 1),
            (self.tx, 1)
        )
        self.connect((self.rx, 0), blocks.null_sink(gr.sizeof_char))
        self.connect((self.rx, 2), blocks.null_sink(gr.sizeof_char))
        self.connect((self.rx, 5), blocks.null_sink(gr.sizeof_gr_complex))
        self.msg_connect((self.rx, "monitor"),
                         (blocks.message_debug(True), "store"))
        self.msg_connect((self.rx, "monitor"), (self.monitor_probe, "in"))
        self.msg_connect((self.tx, "monitor"), (self.msg_debug, "store"))
        return self


# Simulated input and simulated channel
class ofdm_adaptive_sim_src(_ofdm_adaptive_sim):


    def __init__(self, config_dict, run_config_file):
        super().__init__(config_dict, run_config_file)
        self.src = analog.sig_source_b(
            10000, analog.GR_SIN_WAVE, 100, 95, 0, 0)


    def wire_it(self):
        super().wire_it()
        if self.data_bytes is None:
            self.connect((self.src, 0), (self.tx, 0))
        else:
            self.connect((self.src, 0), blocks.head(
                gr.sizeof_char, self.data_bytes), (self.tx, 0))
        return self


# Real input over tun/tap interface and simulated channel
class ofdm_adaptive_sim_tun(_ofdm_adaptive_sim):

    def __init__(self, config_dict, run_config_file):
        super().__init__(config_dict, run_config_file)
        self.tun0 = network.tuntap_pdu("tun0", 500, True)
        self.tun1 = network.tuntap_pdu("tun1", 500, True)
        self.to_pdu = pdu.tagged_stream_to_pdu(gr.types.byte_t, self.rx.packet_length_tag_key)
        self.to_stream = pdu.pdu_to_stream_b(pdu.EARLY_BURST_DROP, 128)

    def wire_it(self):
        super().wire_it()

        self.msg_connect(self.tun0, "pdus", self.to_stream, "pdus")
        if self.data_bytes is None:
            self.connect((self.to_stream, 0), (self.tx, 0))
        else:
            self.connect((self.to_stream, 0), blocks.head(
                gr.sizeof_char, self.data_bytes), (self.tx, 0))
        self.connect((self.rx, 0), self.to_pdu)
        self.msg_connect(self.to_pdu, "pdus", self.tun1, "pdus")
        self.msg_connect(self.to_pdu, "pdus", blocks.message_debug(), "print")
        self.msg_connect(self.tun0, "pdus", blocks.message_debug(), "print")
        self.msg_connect(self.tun1, "pdus", self.tun0, "pdus")

        return self