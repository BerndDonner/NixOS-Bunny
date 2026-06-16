{ config, pkgs, lib, ... }:

{
  # Lockdown must stay small: the normal MCT VM remains the base system.
  # This profile only adds the exam firewall.

  networking.firewall.enable = false;
  networking.nftables.enable = true;

  systemd.services.mct-exam-firewall = {
    description = "MCT exam lockdown firewall";
    wants = [ "network-online.target" ];
    after = [ "network-online.target" ];
    wantedBy = [ "multi-user.target" ];

    path = [
      pkgs.coreutils
      pkgs.gawk
      pkgs.glibc
      pkgs.nftables
    ];

    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
    };

    script = ''
      set -euo pipefail

      AI_HOST="ai.donner-lab.org"
      AI_PORT="11434"

      AI_IP="$(getent ahostsv4 "$AI_HOST" | awk '{ print $1; exit }')"

      if [ -z "$AI_IP" ]; then
        echo "ERROR: Could not resolve $AI_HOST" >&2
        exit 1
      fi

      nft flush ruleset

      nft -f - <<EOF
      table inet mct_exam {
        chain input {
          type filter hook input priority 0; policy drop;

          iifname "lo" accept
          ct state established,related accept

          # DHCP replies for lease renewal.
          udp sport 67 udp dport 68 accept

          reject
        }

        chain output {
          type filter hook output priority 0; policy drop;

          oifname "lo" accept
          ct state established,related accept

          # DHCP requests for lease renewal.
          udp sport 68 udp dport 67 accept

          # DNS is needed to resolve the AI endpoint and for normal resolver behavior.
          udp dport 53 accept
          tcp dport 53 accept

          # NTP keeps timestamps sane during the exam.
          udp dport 123 accept

          # The only exam network service: Ollama/Continue endpoint.
          ip daddr $AI_IP tcp dport $AI_PORT accept

          reject
        }
      }
      EOF

      echo "MCT exam firewall active: $AI_HOST = $AI_IP, allowed TCP port $AI_PORT"
    '';
  };

  environment.systemPackages = with pkgs; [
    nftables
  ];
}
