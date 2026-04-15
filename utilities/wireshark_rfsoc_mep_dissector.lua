--we create our new protocol
local proto_rfsoc_mep = Proto.new("rfsoc_mep", "RFSoC MEP data packet")

local field_sample_idx = ProtoField.uint64("rfsoc_mep.sample_idx", "Sample Index", base.DEC)
local field_sample_rate_numerator = ProtoField.uint64("rfsoc_mep.sample_rate_numerator", "Sample Rate Numerator", base.DEC)
local field_sample_rate_denominator = ProtoField.uint64("rfsoc_mep.sample_rate_denominator", "Sample Rate Denominator", base.DEC)
local field_freq_idx = ProtoField.uint32("rfsoc_mep.freq_idx", "Frequency Index", base.DEC)
local field_num_subchannels = ProtoField.uint32("rfsoc_mep.num_subchannels", "Number of Subchannels", base.DEC)
local field_pkt_samples = ProtoField.uint32("rfsoc_mep.pkt_samples", "Number of Samples", base.DEC)
local field_bits_per_int = ProtoField.uint16("rfsoc_mep.bits_per_int", "Integer Size", base.DEC)
local field_is_complex = ProtoField.bool("rfsoc_mep.is_complex", "Sample type", 1, {"Complex", "Real"}, 0x1)
local field_samples = ProtoField.bytes("rfsoc_mep.samples", "Samples")

proto_rfsoc_mep.fields = {
    field_sample_idx,
    field_sample_rate_numerator,
    field_sample_rate_denominator,
    field_freq_idx,
    field_num_subchannels,
    field_pkt_samples,
    field_bits_per_int,
    field_is_complex,
    field_samples,
}

-- the `dissector()` method is called by Wireshark when parsing our packets
function proto_rfsoc_mep.dissector(buffer, pinfo, tree)
    pinfo.cols.protocol = "RFSoC MEP data"

    -- Entire UDP payload is associated with the protocol
    local payload_tree = tree:add(proto_rfsoc_mep, buffer())

    payload_tree:add_le(field_sample_idx, buffer(0, 8))
    payload_tree:add_le(field_sample_rate_numerator, buffer(8, 8))
    payload_tree:add_le(field_sample_rate_denominator, buffer(16, 8))
    payload_tree:add_le(field_freq_idx, buffer(24, 4))
    payload_tree:add_le(field_num_subchannels, buffer(28, 4))
    payload_tree:add_le(field_pkt_samples, buffer(32, 4))
    payload_tree:add_le(field_bits_per_int, buffer(36, 2))
    payload_tree:add(field_is_complex, buffer(38, 1))
    payload_tree:add(field_samples, buffer(64, buffer:len() - 64))

end

--we register our protocol on UDP port 60131 - 60134
udp_table = DissectorTable.get("udp.port"):add(60134, proto_rfsoc_mep)
udp_table = DissectorTable.get("udp.port"):add(60133, proto_rfsoc_mep)
udp_table = DissectorTable.get("udp.port"):add(60132, proto_rfsoc_mep)
udp_table = DissectorTable.get("udp.port"):add(60131, proto_rfsoc_mep)

