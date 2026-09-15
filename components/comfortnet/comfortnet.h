#pragma once

#include <set>
#include <map>
#include <queue>
#include <variant>
#include <optional>
#include <algorithm>
#include "types.h"
#include "esphome/core/component.h"
#include "esphome/core/helpers.h"
#include "esphome/components/uart/uart.h"

namespace comfortnet {

enum class QueuedMessageType : uint8_t {
  NONE = 0,
  NORMAL = 1,
  ARBITRATION = 2,
};

enum class MessageAckAction : uint8_t {
  NONE = 0,
  ACK = 1,
  NAK = 2,
  UNKNOWN = 3,
};

struct PollQueueEntry {
  NodeType node_type;
  MessageType poll_message;
  bool poll_once;
  std::vector<uint8_t> payload;  // request payload; empty for the argument-less requests (status, sensor data, ...)
  uint32_t interval_ms{0};       // minimum time between repeats; 0 = the component's update_interval
  uint32_t last_sent_ms{0};
  bool ever_sent{false};

  PollQueueEntry(NodeType node_type, MessageType poll_message, bool poll_once, std::vector<uint8_t> payload = {},
                 uint32_t interval_ms = 0)
      : node_type(node_type),
        poll_message(poll_message),
        poll_once(poll_once),
        payload(std::move(payload)),
        interval_ms(interval_ms) {};

  bool operator==(const PollQueueEntry &other) const {
    return node_type == other.node_type && poll_message == other.poll_message && payload == other.payload;
  }
};

struct PendingMessage {
  SendMethod send_method;
  uint8_t send_param_1;
  MessageType packet_type;
  std::vector<uint8_t> payload;

  PendingMessage(SendMethod send_method, uint8_t send_param_1, MessageType packet_type, std::vector<uint8_t> payload)
      : send_method(send_method), send_param_1(send_param_1), packet_type(packet_type), payload(payload) {};
};

struct PendingMessageByCommand : PendingMessage {
  SendMethodControlCommand command_type;

  PendingMessageByCommand(SendMethodControlCommand command_type, MessageType packet_type, std::vector<uint8_t> payload)
      : command_type(command_type),
        PendingMessage(SendMethod::CONTROL_COMMAND, static_cast<uint8_t>(command_type), packet_type, payload) {};
};

struct PendingMessageToType : PendingMessage {
  NodeType node_type;

  PendingMessageToType(NodeType node_type, MessageType packet_type, std::vector<uint8_t> payload)
      : node_type(node_type),
        PendingMessage(SendMethod::NODE_TYPE, static_cast<uint8_t>(node_type), packet_type, payload) {};
};

struct PendingMessageToAddress : PendingMessage {
  NodeAddress dest_address;

  PendingMessageToAddress(NodeAddress dest_address, MessageType packet_type, std::vector<uint8_t> payload)
      : dest_address(dest_address),
        PendingMessage(SendMethod::NODE_ID, static_cast<uint8_t>(dest_address), packet_type, payload) {};
};

struct ComfortnetData {
  NodeType device_type;
  enum class DataType { BOOLEAN, FLOAT, STRING } type;
  using DataVariant = std::variant<bool, float, std::string>;
  DataVariant data;

  ComfortnetData(NodeType device_type, DataType type, DataVariant data)
      : device_type(device_type), type(type), data(data) {};
};

struct ComfortnetCommandData {
  NodeType node_type;
  std::optional<MacAddress> node_mac;
  CommandType cmd_type;
  bool response;
  const uint8_t *payload;
  uint8_t payload_len;

  ComfortnetCommandData(NodeType node_type, std::optional<MacAddress> node_mac, CommandType cmd_type, bool response,
                        const uint8_t *payload, uint8_t payload_len)
      : node_type(node_type),
        node_mac(node_mac),
        cmd_type(cmd_type),
        response(response),
        payload(payload),
        payload_len(payload_len) {};
};

struct ComfortnetPacketData {
  NodeType node_type;
  std::optional<MacAddress> node_mac;
  MessageType packet_type;
  MessageType packet_type_request;
  MessageType packet_type_response;
  const uint8_t *payload;
  uint8_t payload_len;

  ComfortnetPacketData(NodeType node_type, std::optional<MacAddress> node_mac, MessageType packet_type,
                       const uint8_t *payload, uint8_t payload_len)
      : node_type(node_type),
        node_mac(node_mac),
        packet_type(packet_type),
        packet_type_request(PACKET_REQUEST(packet_type)),
        packet_type_response(PACKET_RESPONSE(packet_type)),
        payload(payload),
        payload_len(payload_len) {};
};

struct DBIDDatagram {
  uint8_t dbid_tag;
  uint8_t db_len;
  const uint8_t *data;

  DBIDDatagram(uint8_t dbid_tag, uint8_t db_len, const uint8_t *data)
      : dbid_tag(dbid_tag), db_len(db_len), data(data) {};
};

class Comfortnet : public esphome::Component, public esphome::uart::UARTDevice {
 public:
  void setup() override;
  void loop() override;
  void dump_config() override;

  void set_device_type(uint8_t type) { device_type_ = static_cast<NodeType>(type); }
  void set_ct_version(uint8_t version) { ct_version_ = version; }
  void set_flow_control_pin(esphome::GPIOPin *flow_control_pin) { this->flow_control_pin_ = flow_control_pin; }

  void set_update_interval(uint32_t interval_millis) { update_interval_millis_ = interval_millis; }

  // Listen only: never write to the bus. Set from YAML (listen_only) and switchable at runtime.
  void set_listen_only(bool listen_only) { this->transmit_enabled_ = !listen_only; }
  void set_transmit_enabled(bool enabled);
  bool is_transmit_enabled() const { return this->transmit_enabled_; }
  bool is_network_member() const { return this->node_id_ != static_cast<NodeAddress>(0); }
  // Discovery responses seen from nodes that have no address (source address 0x00).
  uint32_t get_unjoined_discovery_responses() const { return this->unjoined_discovery_responses_; }

  inline void register_listener(std::string sensor_key, std::function<void(ComfortnetData)> callback) {
    std::vector<std::function<void(ComfortnetData)>> *listener_vector = nullptr;
    auto iter = this->listeners_.find(sensor_key);
    if (iter == this->listeners_.end()) {
      listener_vector = &this->listeners_[sensor_key];
    } else {
      listener_vector = &iter->second;
    }
    listener_vector->push_back(callback);
  };
  inline void register_command_listener(CommandType command_type, std::function<void(ComfortnetCommandData)> callback) {
    std::vector<std::function<void(ComfortnetCommandData)>> *listener_vector = nullptr;
    auto iter = this->command_listeners_.find(command_type);
    if (iter == this->command_listeners_.end()) {
      listener_vector = &this->command_listeners_[command_type];
    } else {
      listener_vector = &iter->second;
    }
    listener_vector->push_back(callback);
  };
  inline void register_packet_listener(MessageType message_type, std::function<void(ComfortnetPacketData)> callback) {
    std::vector<std::function<void(ComfortnetPacketData)>> *listener_vector = nullptr;
    auto iter = this->packet_listeners_.find(message_type);
    if (iter == this->packet_listeners_.end()) {
      listener_vector = &this->packet_listeners_[message_type];
    } else {
      listener_vector = &iter->second;
    }
    listener_vector->push_back(callback);
  };

  inline void register_device_polling(NodeType node_type, MessageType poll_message, bool poll_once,
                                      std::vector<uint8_t> payload = {}, uint32_t interval_ms = 0) {
    auto it = std::find_if(polling_queue_.begin(), polling_queue_.end(), [&](const PollQueueEntry &e) {
      return e.node_type == node_type && e.poll_message == poll_message && e.payload == payload;
    });
    if (it == polling_queue_.end()) {
      polling_queue_.emplace_back(node_type, poll_message, poll_once, std::move(payload), interval_ms);
    } else {
      if (it->poll_once && !poll_once) {
        // If the existing request says to poll once, but this one says to repeatedly pull, reconfigure the request.
        it->poll_once = poll_once;
      }
      if (interval_ms != 0 && (it->interval_ms == 0 || interval_ms < it->interval_ms)) {
        it->interval_ms = interval_ms;  // the fastest explicit interval requested for this poll wins
      }
    }
  };
  /**
   * Moves the given device to the end of the poll priority list
   */
  inline void device_poll_to_end(NodeType node_type, MessageType poll_message) {
    auto it = std::find_if(polling_queue_.begin(), polling_queue_.end(), [&](const PollQueueEntry &e) {
      return e.node_type == node_type && e.poll_message == poll_message;
    });
    if (it == polling_queue_.end()) {
      return;
    }
    if (it->poll_once) {
      // erase() invalidates `it`, so it must not be rotated afterwards (upstream did, which is undefined behaviour).
      polling_queue_.erase(it);
      return;
    }
    std::rotate(it, it + 1, polling_queue_.end());
  };

  inline void read_mdi(const uint8_t *data, uint8_t data_len, std::vector<DBIDDatagram> *parsed) {
    uint8_t i = 0;
    while (i < data_len) {
      if (i + 2 >= data_len) {
        return;
      }
      uint8_t tag = data[i++];
      uint8_t len = data[i++];
      if (i + len > data_len) {
        return;
      }
      parsed->emplace_back(tag, len, data + i);
      i += len;
    }
  };

  inline void queue_message(PendingMessage message) { pending_messages_.push(message); };

 protected:
  uint32_t update_interval_millis_{30000};
  void read_buffer_(int bytes_available, uint32_t now);
  void handle_message_(bool is_tx, uint32_t now);
  uint16_t calculate_crc_(const uint8_t *data, uint8_t data_len);
  uint32_t generate_slot_delay_();
  void set_node_list_(const uint8_t *data, uint8_t data_len);
  int due_poll_index_(uint32_t now) const;
  inline NodeType get_node_type_(NodeAddress address) {
    uint8_t addr = static_cast<uint8_t>(address);
    if (addr >= MAX_PAYLOAD_SIZE) {
      return NodeType::ANY;
    }
    return this->node_list_[addr];
  }
  inline std::optional<MacAddress> get_node_mac_(NodeAddress address) {
    uint8_t addr = static_cast<uint8_t>(address);
    if (addr >= MAX_PAYLOAD_SIZE) {
      return std::nullopt;
    }
    return this->node_mac_list_[addr];
  }
  void disconnect_();

  inline void call_listener_(std::string sensor_key, ComfortnetData data) {
    auto iter = this->listeners_.find(sensor_key);
    if (iter != this->listeners_.end()) {
      std::vector<std::function<void(ComfortnetData)>> listener_vector = iter->second;
      for (auto &callback : listener_vector) {
        callback(data);
      }
    }
  }
  inline void call_command_listener_(ComfortnetCommandData data) {
    auto iter = this->command_listeners_.find(data.cmd_type);
    if (iter != this->command_listeners_.end()) {
      std::vector<std::function<void(ComfortnetCommandData)>> listener_vector = iter->second;
      for (auto &callback : listener_vector) {
        callback(data);
      }
    }
  }
  inline void call_packet_listener_(ComfortnetPacketData data) {
    auto iter = this->packet_listeners_.find(data.packet_type);
    if (iter != this->packet_listeners_.end()) {
      std::vector<std::function<void(ComfortnetPacketData)>> listener_vector = iter->second;
      for (auto &callback : listener_vector) {
        callback(data);
      }
    }
  }

  inline void transmit_message_(NodeAddress dst_adr, NodeAddress src_adr, Subnet subnet, SendMethod send_method,
                                uint8_t send_param_1, uint8_t send_param_2, NodeType src_node_type,
                                MessageType msg_type, uint8_t packet_num, const std::vector<uint8_t> &data,
                                bool require_arbitration = false) {
    write_message_to_buffer_(tx_message_, dst_adr, src_adr, subnet, send_method, send_param_1, send_param_2,
                             src_node_type, msg_type, packet_num, data.data(), data.size(), true, require_arbitration);
  }
  inline void transmit_message_(NodeAddress dst_adr, NodeAddress src_adr, Subnet subnet, SendMethod send_method,
                                uint8_t send_param_1, uint8_t send_param_2, NodeType src_node_type,
                                MessageType msg_type, uint8_t packet_num, const uint8_t *data, uint8_t data_len,
                                bool require_arbitration = false) {
    write_message_to_buffer_(tx_message_, dst_adr, src_adr, subnet, send_method, send_param_1, send_param_2,
                             src_node_type, msg_type, packet_num, data, data_len, true, require_arbitration);
  }
  void write_message_to_buffer_(std::vector<uint8_t> &buffer, NodeAddress dst_adr, NodeAddress src_adr, Subnet subnet,
                                SendMethod send_method, uint8_t send_param_1, uint8_t send_param_2,
                                NodeType src_node_type, MessageType msg_type, uint8_t packet_num, const uint8_t *data,
                                uint8_t data_len, bool queue_send, bool require_arbitration);
  esphome::GPIOPin *flow_control_pin_{nullptr};

  std::vector<uint8_t> rx_message_;
  std::vector<uint8_t> tx_message_;
  std::vector<uint8_t> r2r_reply_;

  uint32_t last_read_time_{0};                                 // Last time any data was read
  uint32_t last_address_confirm_time_{0};                      // Last time our address was confirmed
  uint32_t slot_delay_{0};                                     // Calculated slot delay when we are arbitrating
  QueuedMessageType message_queued_{QueuedMessageType::NONE};  // Whether we should arbitrate, or are sending normally
  bool awaiting_discovery_{false};                                    // Whether we are in the discovery process
  uint32_t discovery_response_ms_{0};  // When we last answered discovery while unjoined
  bool has_won_token_broadcast_{false};                               // Devices can only win token offer once per dataflow
  bool transmit_enabled_{true};               // false = listen only, never write to the bus
  uint32_t unjoined_discovery_responses_{0};  // discovery responses seen from address 0x00
  uint32_t pending_sent_ms_{0};               // when the front pending message was last transmitted
  uint8_t pending_attempts_{0};               // how many times the front pending message has been transmitted

  MacAddress mac_address_{};
  uint8_t ct_version_{2};  // Numerical value representing the desired CT version (either 1 or 2)
  NodeType device_type_{NodeType::DIAGNOSTIC_DEVICE};
  NodeAddress node_id_{static_cast<NodeAddress>(0)};
  Subnet subnet_{Subnet::BROADCAST};
  SessionId session_id_{};

  std::queue<PendingMessage> pending_messages_;
  std::vector<PollQueueEntry> polling_queue_;

  uint8_t node_list_size_ = 0;
  NodeType node_list_[MAX_PAYLOAD_SIZE]{};      // zero = ANY until the coordinator sends us the node list
  MacAddress node_mac_list_[MAX_PAYLOAD_SIZE]{};
  std::map<NodeType, std::vector<uint8_t>> network_shared_data_;

  std::map<std::string, std::vector<std::function<void(ComfortnetData)>>> listeners_;
  std::map<CommandType, std::vector<std::function<void(ComfortnetCommandData)>>> command_listeners_;
  std::map<MessageType, std::vector<std::function<void(ComfortnetPacketData)>>> packet_listeners_;
};

class ComfortnetClient {
 public:
  inline void set_comfortnet_parent(Comfortnet *parent) { this->parent_ = parent; };

 protected:
  Comfortnet *parent_{nullptr};
};

}  // namespace comfortnet
