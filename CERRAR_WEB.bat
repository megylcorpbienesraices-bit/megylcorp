@echo off
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -Command "$c=Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue; if($c){$ids=$c.OwningProcess | Sort-Object -Unique; foreach($id in $ids){try{$p=Get-Process -Id $id -ErrorAction Stop; if($p.ProcessName -match 'python|py'){Write-Host ('Cerrando servidor anterior PID '+$id+'...'); Stop-Process -Id $id -Force}else{Write-Host ('Puerto 8000 ocupado por '+$p.ProcessName+' PID '+$id+'. No se cerrara automaticamente.')}}catch{}}}"
timeout /t 1 /nobreak >nul
endlocal
