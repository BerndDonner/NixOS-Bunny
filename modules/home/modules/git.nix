{ config, pkgs, lib, inputs, ... }:

{
  programs.git = {
    enable = true;
    package = pkgs.gitFull;

    settings = {

      credential.helper = "kwallet";
      init.defaultBranch = "master";

      pull.rebase = true;
      rebase.autoStash = true;
      fetch.prune = true;
      rerere.enabled = true;
      merge.ff = "only";

      alias = {
        st = "status -sb";
        lg = "log --oneline --graph --decorate --all";
        br = "branch -vv";

        pub = ''!f(){ b=$(git rev-parse --abbrev-ref HEAD); git push -u origin "$b"; }; f'';

        up = ''
          !f(){             set -e;             force=0;             if [ "$1" = "--force" ] || [ "$1" = "-f" ]; then force=1; shift; fi;             u=$(git rev-parse --abbrev-ref --symbolic-full-name @{u} 2>/dev/null) || {               echo "ERROR: No upstream configured. Run: git pub"; exit 1; };             ahead=$(git rev-list --count @{u}..HEAD);             if [ "$ahead" -gt 0 ] && [ "$force" -eq 0 ]; then               echo "ERROR: You have $ahead local commit(s) that are NOT pushed.";               echo "       Run: git pub   or (explicitly): git up --force";               exit 1;             fi;             if [ "$ahead" -gt 0 ] && [ "$force" -eq 1 ]; then               b=$(git rev-parse --abbrev-ref HEAD);               echo "⚠️  WARNING: git up --force";               echo "   Branch:   $b";               echo "   Upstream: $u";               echo "   Status:   ahead by $ahead commit(s) (not pushed)";               echo "   Action:   fetch + rebase --autostash onto upstream";               echo "   Note:     If you push afterwards, you may need --force-with-lease.";               echo "             Conflicts are normal here. To abort: git abort-op";               echo "";             fi;             git fetch --prune;             git rebase --autostash @{u};           }; f
        '';

        current = ''
          !f(){             if test -d "$(git rev-parse --git-path rebase-apply)" -o -d "$(git rev-parse --git-path rebase-merge)"; then               git checkout --theirs -- "$@";             else               git checkout --ours -- "$@";             fi;           }; f
        '';
        incoming = ''
          !f(){             if test -d "$(git rev-parse --git-path rebase-apply)" -o -d "$(git rev-parse --git-path rebase-merge)"; then               git checkout --ours -- "$@";             else               git checkout --theirs -- "$@";             fi;           }; f
        '';

        undo    = "reset --soft HEAD~1";
        discard = "reset --hard";

        reset-to-remote = ''
          !f(){ set -e;             b="$1";             if [ -z "$b" ]; then b=$(git rev-parse --abbrev-ref HEAD); fi;             if [ "$b" = master ] || [ "$b" = main ]; then               echo "ERROR: reset-to-remote nicht auf '$b' ausfuehren."; exit 1;             fi;             git fetch --all --prune;             git switch "$b";             if git show-ref --verify --quiet "refs/remotes/origin/$b"; then               r="origin/$b";             else               u=$(git rev-parse --abbrev-ref --symbolic-full-name @{u} 2>/dev/null || true);               if [ -n "$u" ]; then r="$u"; else                 echo "ERROR: Weder origin/$b noch upstream gefunden."; exit 1; fi;             fi;             git reset --hard "$r";           }; f
        '';

        abort-op = ''
          !f(){             set +e;             try(){               op="$1"; shift;               "$@" >/dev/null 2>&1;               rc=$?;               if [ $rc -eq 0 ]; then                 echo "OK: aborted $op";                 exit 0;               fi;             };             try rebase      git rebase --abort;             try merge       git merge --abort;             try cherry-pick git cherry-pick --abort;             try revert      git revert --abort;             try am          git am --abort;             try bisect      git bisect reset;             echo "INFO: no in-progress operation detected.";           }; f
        '';
      };
    };
  };
}
