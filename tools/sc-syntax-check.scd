// Syntax gate for the .scd sources.
//
// compileFile PARSES a file the way `.load` does but does NOT execute it, so the engine
// file can be checked without booting a server. Both weaker gates were tried and both let
// a real break through:
//
//   - `interpreter.compile(wholeFileAsString)` treats the text as one function body, where
//     the "all vars first" rule does not apply the same way. It passed a file that `.load`
//     rejected, and that file was deployed and silently killed the engine.
//   - `.load` inside a try/catch does NOT throw on a syntax error. It prints the error and
//     returns normally, so the catch never fires and the check reports success.
//
// Any output containing "syntax error" means the file is broken, whatever this prints.
(
var files = thisProcess.argv;
var bad = 0;
files.do { |f|
    var fn = thisProcess.interpreter.compileFile(f);
    if (fn.isNil) { bad = bad + 1; ("SYNTAX FAIL " ++ f).postln } { ("SYNTAX OK   " ++ f).postln };
};
("SYNTAX SUMMARY " ++ bad ++ " bad").postln;
0.exit;
)
