// On-device boot wrapper for the Granola SC engine (Move takeover).
//
// The bundled sclang was built with prefix /usr/local, so its default scsynth path and
// resourceDir do not exist on the Move. Point the Server at the bundle run-engine.sh
// picked (GR_SC_HOME), then hand off to granola-engine.scd, which boots the server.
//
// GRANOLA SHIPS NO SC BUNDLE OF ITS OWN. Every takeover on this box carries the same
// ~200-plugin SuperCollider build; a second copy would be pure duplication. run-engine.sh
// resolves GR_SC_HOME by probing the bundles already installed, and the engine's only
// hard requirement beyond core SC is JPverb (sc3-plugins), which those bundles have.
(
var home = "GR_SC_HOME".getenv ?? "/data/UserData/poundhard";
var dir = "GR_HOME".getenv ?? "/data/UserData/granola";
// SERVER CHOICE — GR_THREADS (set by run-engine.sh):
//   >0  -> supernova, the multicore server, with that many DSP threads
//   0   -> scsynth (single-threaded)
// supernova loads ONLY the *_supernova.so plugin variants; the bundles ship both sets.
var tstr = "GR_THREADS".getenv;
var threads = tstr.notNil.if({ tstr.asInteger }, { 0 });
~grSupernova = threads > 0;
Server.program = home +/+ (~grSupernova.if({ "bin/supernova" }, { "bin/scsynth" }));
~grSupernova.if { Server.default.options.threads = threads };
// Granola's OWN plugin dir comes first, then the shared bundle's. ugenPluginsPath
// REPLACES the search path rather than adding to it, so both have to be named or the
// Airwindows UGens (ours) or the sc3-plugins ones (the bundle's) would go missing.
Server.default.options.ugenPluginsPath = [dir +/+ "plugins", home +/+ "plugins"];

// Audio config. Set here rather than left at SC's defaults: the Move's shadow JACK
// runs at 44100 with a 128-frame period, and a server whose block size does not match
// the period costs an extra buffer of latency and invites xruns. memSize is raised for
// the delay lines and the reverb tank.
Server.default.options.sampleRate = ("GR_SR".getenv ? "44100").asInteger;
Server.default.options.blockSize = ("GR_BLOCK".getenv ? "128").asInteger;
Server.default.options.numOutputBusChannels = 2;
Server.default.options.numInputBusChannels = 2;
Server.default.options.memSize = 256 * 1024;
// Eight stereo tracks = 16 sample buffers, plus the four grain windows at 201-204 and
// the recorder at 300; the default 1024 is ample, but say so explicitly.
Server.default.options.numBuffers = 1024;

(dir +/+ "sc/granola-engine.scd").load;
)
