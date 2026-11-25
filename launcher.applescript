-- AA Scope: пуска програмата тихо (без Terminal) и отваря страницата
on run
	set appDir to (POSIX path of (path to home folder)) & "station/aa-scope"
	set cmd to "cd " & quoted form of appDir & " && " & ¬
		"if ! /usr/sbin/lsof -nP -iTCP:8765 -sTCP:LISTEN >/dev/null 2>&1; then " & ¬
		"/usr/bin/nohup /usr/bin/python3 -u aa_scope.py --no-browser > aa_scope.log 2>&1 < /dev/null & " & ¬
		"fi; " & ¬
		"for i in $(/usr/bin/seq 1 30); do /usr/sbin/lsof -nP -iTCP:8765 -sTCP:LISTEN >/dev/null 2>&1 && exit 0; /bin/sleep 0.3; done; exit 1"
	try
		do shell script cmd
		open location "http://127.0.0.1:8765/"
	on error
		display dialog "AA Scope не тръгна. Подробности в " & appDir & "/aa_scope.log" buttons {"OK"} default button 1 with icon caution
	end try
end run
