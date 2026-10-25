# Forge desktop benchmark form: a real Win32/WinForms window with full UI Automation support.
# Writes the submitted values to $env:FORGE_FORM_OUT as JSON so the benchmark has ground truth.
param([string]$Title = "Forge Test Form")
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$f = New-Object Windows.Forms.Form
$f.Text = $Title; $f.Size = New-Object Drawing.Size(460, 420); $f.StartPosition = "CenterScreen"; $f.TopMost = $false
function Add-Label($text, $y) { $l = New-Object Windows.Forms.Label; $l.Text = $text; $l.Location = New-Object Drawing.Point(20, $y); $l.AutoSize = $true; $f.Controls.Add($l) }
function Add-Box($name, $y) { $t = New-Object Windows.Forms.TextBox; $t.AccessibleName = $name; $t.Name = ($name -replace '\W',''); $t.Location = New-Object Drawing.Point(150, $y); $t.Width = 260; $f.Controls.Add($t); $t }
Add-Label "Full name" 22;  $name  = Add-Box "Full name" 20
Add-Label "Email" 57;      $email = Add-Box "Email address" 55
Add-Label "Phone" 92;      $phone = Add-Box "Phone number" 90
Add-Label "Role" 127
$role = New-Object Windows.Forms.ComboBox; $role.AccessibleName = "Role applied for"; $role.Name = "Role"; $role.DropDownStyle = "DropDownList"
[void]$role.Items.AddRange(@("Graduate Software Engineer", "Data Analyst", "IT Support")); $role.Location = New-Object Drawing.Point(150, 125); $role.Width = 260; $f.Controls.Add($role)
$work = New-Object Windows.Forms.CheckBox; $work.Text = "I have Australian work rights"; $work.Name = "WorkRights"; $work.Location = New-Object Drawing.Point(20, 165); $work.AutoSize = $true; $f.Controls.Add($work)
$conf = New-Object Windows.Forms.CheckBox; $conf.Text = "I confirm these details are true"; $conf.Name = "Confirm"; $conf.Location = New-Object Drawing.Point(20, 195); $conf.AutoSize = $true; $f.Controls.Add($conf)
$status = New-Object Windows.Forms.Label; $status.Name = "Status"; $status.Text = "Status: editing"; $status.Location = New-Object Drawing.Point(20, 330); $status.AutoSize = $true; $f.Controls.Add($status)
function Save($kind) {
  $o = [ordered]@{ action = $kind; name = $name.Text; email = $email.Text; phone = $phone.Text; role = [string]$role.SelectedItem; work_rights = $work.Checked; confirmed = $conf.Checked }
  if ($env:FORGE_FORM_OUT) { ($o | ConvertTo-Json -Compress) | Set-Content -Encoding utf8 $env:FORGE_FORM_OUT }
  $status.Text = "Status: $kind"
}
function Add-Btn($text, $x, $y, $action) { $b = New-Object Windows.Forms.Button; $b.Text = $text; $b.Name = ($text -replace '\W',''); $b.Location = New-Object Drawing.Point($x, $y); $b.Width = 120; $b.Add_Click($action); $f.Controls.Add($b) }
Add-Btn "Save draft" 20 240 { Save "saved_draft" }
Add-Btn "Clear form" 150 240 { $name.Text = ""; $email.Text = ""; $phone.Text = ""; $status.Text = "Status: cleared" }
Add-Btn "Delete record" 280 240 { Save "deleted" }
Add-Btn "Help" 20 275 { $status.Text = "Status: help shown" }
Add-Btn "Submit application" 150 275 { Save "submitted" }
[void]$f.ShowDialog()
