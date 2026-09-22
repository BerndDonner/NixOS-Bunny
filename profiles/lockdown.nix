{ config, pkgs, lib, forgejoHost, ... }:

{
  # Lockdown must stay small: the normal MCT VM remains the base system.
  # This profile only adds the exam firewall.
  #
  # Intended operation:
  #   systemctl start mct-exam-firewall  -> activate exam lockdown
  #   systemctl stop  mct-exam-firewall  -> open the network for fallback submission
  #
  # The start path is fail-closed:
  #   - A restrictive base ruleset is installed before DNS is queried.
  #   - AI and Forgejo are resolved independently.
  #   - If either hostname cannot be resolved, that service stays blocked while
  #     the rest of the lockdown remains active.

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
      coreutils  # timeout, sleep, sort
      gawk       # awk
      getent     # getent ahostsv4/ahostsv6
      nftables   # nft
      gnused      # sed
    ];

    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;

      # Stopping the service intentionally opens the network again. This is the
      # supervised fallback path if Git submission is unavailable.
      ExecStop = "${pkgs.nftables}/bin/nft flush ruleset";
    };

    script = ''
      set -euo pipefail

      AI_HOST="ai.donner-lab.org"
      AI_PORT="11434"
      FORGEJO_HOST=${lib.escapeShellArg forgejoHost}
      FORGEJO_PORT="443"

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

      apply_rules() {
        local endpoint_rules="$1"
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

          # DNS is required only for the configured exam endpoints and normal
          # resolver behavior. Restrict it to the active nameservers.
          ''${dns_rules}

          # NTP keeps commit and filesystem timestamps sane during the exam.
          udp dport 123 accept

          # Exact IP/port rules generated from the current DNS answers for the
          # two allowed exam services. No other HTTPS destination is opened.
          ''${endpoint_rules}

          reject
        }
      }
      NFT_EOF
      }

      resolve_host() {
        local host="$1"
        local attempt=""
        local result=""

        for attempt in 1 2 3 4 5 6; do
          result="$({
            timeout 8s getent ahostsv4 "$host" 2>/dev/null \
              | awk '$1 ~ /^[0-9]+([.][0-9]+){3}$/ { print "4 " $1 }'
            timeout 8s getent ahostsv6 "$host" 2>/dev/null \
              | awk '$1 ~ /:/ { print "6 " $1 }'
          } | sort -u || true)"

          if [ -n "$result" ]; then
            printf '%s\n' "$result"
            return 0
          fi

          echo "WARN: Could not resolve $host (attempt $attempt/6)." >&2
          sleep 5
        done

        return 1
      }

      append_endpoint_rules() {
        local resolved="$1"
        local port="$2"
        local rules=""
        local family=""
        local address=""

        while read -r family address; do
          [ -n "$family" ] || continue
          case "$family" in
            4) rules="''${rules}          ip daddr $address tcp dport $port accept\n" ;;
            6) rules="''${rules}          ip6 daddr $address tcp dport $port accept\n" ;;
            *) echo "WARN: Ignoring unexpected address family '$family'." >&2 ;;
          esac
        done <<< "$resolved"

        printf '%b' "$rules"
      }

      # Prefer starting after DHCP/resolvconf has written the active nameservers.
      # If this fails, continue fail-closed with no DNS rules.
      wait_for_dns_config || echo "WARN: No nameserver found in /etc/resolv.conf. DNS will remain blocked." >&2

      # Install the restrictive ruleset before any DNS operation that may block.
      apply_rules ""

      AI_RESOLVED="$(resolve_host "$AI_HOST" || true)"
      FORGEJO_RESOLVED="$(resolve_host "$FORGEJO_HOST" || true)"
      ENDPOINT_RULES=""

      if [ -n "$AI_RESOLVED" ]; then
        ENDPOINT_RULES="''${ENDPOINT_RULES}$(append_endpoint_rules "$AI_RESOLVED" "$AI_PORT")"
      else
        echo "WARN: $AI_HOST unresolved; Ollama remains blocked." >&2
      fi

      if [ -n "$FORGEJO_RESOLVED" ]; then
        ENDPOINT_RULES="''${ENDPOINT_RULES}$(append_endpoint_rules "$FORGEJO_RESOLVED" "$FORGEJO_PORT")"
      else
        echo "WARN: $FORGEJO_HOST unresolved; Forgejo remains blocked." >&2
      fi

      apply_rules "$ENDPOINT_RULES"

      echo "MCT exam firewall active."
      if [ -n "$AI_RESOLVED" ]; then
        echo "  allowed: $AI_HOST:$AI_PORT"
        printf '%s\n' "$AI_RESOLVED" | sed 's/^/           /'
      fi
      if [ -n "$FORGEJO_RESOLVED" ]; then
        echo "  allowed: $FORGEJO_HOST:$FORGEJO_PORT"
        printf '%s\n' "$FORGEJO_RESOLVED" | sed 's/^/           /'
      fi
    '';
  };

  environment.systemPackages = with pkgs; [
    nftables
  ];
}
