{
  # Fallback values if hosts/<hostname>.nix is missing. The deliberately
  # invalid MCT identity makes course bootstrap/hooks fail closed.
  gitName  = "Student";
  gitEmail = "student@example.invalid";
  forgejo  = "UNCONFIGURED";
  course   = "UNCONFIGURED";
}
