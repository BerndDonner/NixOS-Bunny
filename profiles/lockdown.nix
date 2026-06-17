{ config, pkgs, lib, ... }:

{
  # Lockdown must stay small: the normal MCT VM remains the base system.
  # This profile only adds the exam firewall.
  #
  # Intended operation:
  #   systemctl start mct-exam-firewall  -> activate exam lockdown
  #   systemctl stop  mct-exam-firewall  -> open the network for submission
  #
  # The start path is fail-closed:
  #   - A restrictive base ruleset is installed before DNS is queried.
  #   - If ai.donner-lab.org cannot be resolved, the VM stays locked down.
  #   - Restart the service after starting the AI server to add the AI allow rule.

  networking.firewall.enable = false;
  networking.nftables.enable = true;

  systemd.services.mct-exam-firewall = {
    description = "MCT exam lockdown firewall";
    wants = [ "network-online.target" ];
    after = [
      "network-online.target"
      "NetworkManager.service"
      "systemd-networkd.service"
    ];
    wantedBy = [ "multi-user.target" ];

    path = with pkgs; [
      coreutils  # timeout, sleep
      gawk       # awk
      getent     # getent ahostsv4
      nftables   # nft
    ];

    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;

      # Stopping the service intentionally opens the network again, for example
      # during the supervised submission phase at the end of the exam.
      ExecStop = "${pkgs.nftables}/bin/nft flush ruleset";
    };

    script = ''
      set -euo pipefail

      AI_HOST="ai.donner-lab.org"
      AI_PORT="11434"


      wait_for_dns_config() {
        local attempt=""

        for attempt in 1 2 3 4 5 6 7 8 9 10; do
          if awk '$1 == "nameserver" { found=1 } END { exit !found }' /etc/resolv.conf 2>/dev/null; then
            return 0
          fi

          echo "WARN: /etc/resolv.conf has no nameserver yet (attempt $attempt/10)." >&2
          sleep 1
        done

        return 1
      }

      build_dns_rules() {
        awk '
          $1 == "nameserver" && $2 ~ /^[0-9]+([.][0-9]+){3}$/ {
            print "          ip daddr " $2 " udp dport 53 accept"
            print "          ip daddr " $2 " tcp dport 53 accept"
          }

          $1 == "nameserver" && $2 ~ /:/ {
            print "          ip6 daddr " $2 " udp dport 53 accept"
            print "          ip6 daddr " $2 " tcp dport 53 accept"
          }
        ' /etc/resolv.conf 2>/dev/null
      }

      apply_base_rules() {
        local dns_rules=""

        dns_rules="$(build_dns_rules)"

        nft flush ruleset

        nft -f - <<NFT_EOF
      table inet mct_exam {
        chain input {
          type filter hook input priority 0; policy drop;

          iifname "lo" accept
          ct state established,related accept

          # DHCP replies for IPv4 lease renewal.
          udp sport 67 udp dport 68 accept

          # Minimal ICMPv6 needed for IPv6 Neighbor Discovery and error handling.
          icmpv6 type { nd-neighbor-solicit, nd-neighbor-advert, nd-router-advert, packet-too-big, time-exceeded, parameter-problem } accept

          reject
        }

        chain output {
          type filter hook output priority 0; policy drop;

          oifname "lo" accept
          ct state established,related accept

          # DHCP requests for IPv4 lease renewal.
          udp sport 68 udp dport 67 accept

          # Minimal ICMPv6 needed for IPv6 Neighbor Discovery and error handling.
          icmpv6 type { nd-neighbor-solicit, nd-neighbor-advert, nd-router-solicit, packet-too-big, time-exceeded, parameter-problem } accept

          # DNS is required to resolve the AI endpoint.
          # Allow only the nameservers currently configured by DHCP/resolvconf.
          ''${dns_rules}

          # NTP keeps timestamps sane during the exam.
          udp dport 123 accept

          reject
        }
      }
      NFT_EOF
      }

      apply_ai_rules() {
        local ai_ip="$1"
        local dns_rules=""

        dns_rules="$(build_dns_rules)"

        nft flush ruleset

        nft -f - <<NFT_EOF
      table inet mct_exam {
        chain input {
          type filter hook input priority 0; policy drop;

          iifname "lo" accept
          ct state established,related accept

          # DHCP replies for IPv4 lease renewal.
          udp sport 67 udp dport 68 accept

          # Minimal ICMPv6 needed for IPv6 Neighbor Discovery and error handling.
          icmpv6 type { nd-neighbor-solicit, nd-neighbor-advert, nd-router-advert, packet-too-big, time-exceeded, parameter-problem } accept

          reject
        }

        chain output {
          type filter hook output priority 0; policy drop;

          oifname "lo" accept
          ct state established,related accept

          # DHCP requests for IPv4 lease renewal.
          udp sport 68 udp dport 67 accept

          # Minimal ICMPv6 needed for IPv6 Neighbor Discovery and error handling.
          icmpv6 type { nd-neighbor-solicit, nd-neighbor-advert, nd-router-solicit, packet-too-big, time-exceeded, parameter-problem } accept

          # DNS is required to resolve the AI endpoint and for normal resolver behavior.
          # Allow only the nameservers currently configured by DHCP/resolvconf.
          ''${dns_rules}

          # NTP keeps timestamps sane during the exam.
          udp dport 123 accept

          # The only exam network service: Ollama/Continue endpoint.
          ip daddr $ai_ip tcp dport $AI_PORT accept

          reject
        }
      }
      NFT_EOF
      }

      resolve_ai_ip() {
        local attempt=""
        local ai_ip=""

        for attempt in 1 2 3 4 5 6; do
          ai_ip="$(timeout 8s getent ahostsv4 "$AI_HOST" 2>/dev/null \
            | awk '$1 ~ /^[0-9]+([.][0-9]+){3}$/ { print $1; exit }' || true)"

          if [ -n "$ai_ip" ]; then
            printf '%s\n' "$ai_ip"
            return 0
          fi

          echo "WARN: Could not resolve $AI_HOST (attempt $attempt/6)." >&2
          sleep 5
        done

        return 1
      }

      # Prefer starting after DHCP/resolvconf has written the active nameservers.
      # If this fails, continue fail-closed with no DNS rules.
      wait_for_dns_config || echo "WARN: No nameserver found in /etc/resolv.conf. DNS will remain blocked." >&2

      # Fail closed: install a restrictive base ruleset before doing anything
      # that may block, time out, or fail.
      apply_base_rules

      AI_IP="$(resolve_ai_ip || true)"

      if [ -z "$AI_IP" ]; then
        echo "WARN: $AI_HOST could not be resolved. Exam firewall remains active without AI access." >&2
        echo "WARN: Start the AI server and run: sudo systemctl restart mct-exam-firewall" >&2
        exit 0
      fi

      apply_ai_rules "$AI_IP"

      echo "MCT exam firewall active: $AI_HOST = $AI_IP, allowed TCP port $AI_PORT"
    '';
  };

  environment.systemPackages = with pkgs; [
    nftables
  ];
}
