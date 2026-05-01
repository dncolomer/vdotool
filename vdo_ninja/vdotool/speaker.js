/*
 * vdotool speaker module.
 *
 * Loaded in the viewer page alongside capture.js when the URL is
 * ?vdotool=1&view=1&sessionId=<sid>. Its job is to play the agent's
 * TTS clips (MP3s in audio_out/<clip>.mp3, listed in
 * audio_out/queue.json) on the phone that's pushing camera into the
 * same VDO.Ninja room.
 *
 * Architecture:
 *   1. Create a synthetic audio MediaStream in the main viewer tab via
 *      a persistent AudioContext + MediaStreamAudioDestinationNode. TTS
 *      clips are decoded into AudioBuffers and played through this
 *      destination node; its outbound MediaStreamTrack becomes the
 *      agent's "microphone" track.
 *   2. Create a hidden iframe that hosts a second VDO.Ninja PUSHER in
 *      the same room (?push=<sid>_agent). The phone, already a peer
 *      in the same room, plays the audio from this pusher automatically
 *      via WebRTC — no extra phone-side setup needed.
 *   3. The iframe is built with `srcdoc` so we can inject an inline
 *      <script> that overrides `navigator.mediaDevices.getUserMedia`
 *      BEFORE any VDO.Ninja script runs, preventing a race where the
 *      iframe's VDO.Ninja call to getUserMedia beats our override and
 *      publishes a blank fake-device track.
 *   4. The inline override is installed in the iframe window, then the
 *      iframe redirects via `location.replace()` to the real viewer URL
 *      (./index.html?...) so we inherit same-origin + relative paths.
 *
 * Queue polling:
 *   - GET /vdotool/audio-queue?sessionId=<sid>
 *   - for each new pending clip: fetch /vdotool/audio/<sid>/<clip>,
 *     decodeAudioData, play on the destination node.
 *   - on ended: POST /vdotool/audio-ack?sessionId=...&clip=...
 *   - on `interrupt_epoch_ms` advance: stop current clip immediately.
 *
 * Defensive: every failure is caught and logged; we never break the
 * host viewer.
 */
(function (globalScope) {
	'use strict';

	var POLL_INTERVAL_MS = 1500;
	var WRITER_PATH_QUEUE = '/vdotool/audio-queue';
	var WRITER_PATH_CLIP = '/vdotool/audio/';
	var WRITER_PATH_ACK = '/vdotool/audio-ack';

	function log() {
		try {
			var args = Array.prototype.slice.call(arguments);
			args.unshift('[vdotool speaker]');
			console.log.apply(console, args);
		} catch (_e) {}
	}

	function sessionIdValid(sid) {
		return typeof sid === 'string' && /^[A-Za-z0-9_-]{8,64}$/.test(sid);
	}

	// ------------------------------------------------------------------
	// Companion pusher iframe.
	//
	// Architecture note: previous versions created the AudioContext +
	// MediaStreamDestinationNode in the parent and tried to postMessage
	// the resulting MediaStream into the iframe. That doesn't work in
	// Chromium — `MediaStream` is not structured-cloneable across
	// realms and `iframe.contentWindow.postMessage(stream, ...)` throws
	// DataCloneError. The iframe never receives the stream, the
	// VDO.Ninja inside it hangs forever waiting for getUserMedia, and
	// no audio reaches the phone.
	//
	// Fix: AudioContext + MediaStreamDestinationNode live INSIDE the
	// iframe, where VDO.Ninja can pick the stream up directly. The
	// parent only ships clip bytes (ArrayBuffer — structured-cloneable)
	// to the iframe via postMessage. The iframe decodes and plays them
	// into its own destination node.
	// ------------------------------------------------------------------

	function buildIframeSrcdoc(roomId, agentStreamId, sessionId, origin) {
		var targetUrl = origin + '/?room=' + encodeURIComponent(roomId)
			+ '&push=' + encodeURIComponent(agentStreamId)
			+ '&autostart=1'
			+ '&cleanoutput=1'
			+ '&videodevice=0'
			+ '&audiodevice=1'
			+ '&noaudioprocessing=1'
			+ '&vdotoolSpeaker=1';

		// The bootstrap script:
		//   1. Builds the AudioContext + MediaStreamDestinationNode
		//      here in the iframe; keeps a silent oscillator on it so
		//      the track stays "live" while idle (some browsers
		//      otherwise mark idle MediaStreamTracks as 'ended' and
		//      VDO.Ninja drops them).
		//   2. Overrides navigator.mediaDevices.getUserMedia so when
		//      VDO.Ninja's pusher requests the "microphone" it gets
		//      our synthetic stream instead.
		//   3. Exposes window.__vtPlay(arrayBuffer) so the parent can
		//      schedule decoded clips one at a time.
		//   4. Notifies the parent it's ready, then redirects to the
		//      pusher URL (which loads VDO.Ninja, which calls gUM,
		//      which gets our stream).
		var bootstrap = ''
			+ '<!DOCTYPE html><html><head><meta charset="utf-8">'
			+ '<title>vdotool speaker</title></head><body>'
			+ '<script>'
			+ '(function(){'
			+ '  var ctx = new (window.AudioContext || window.webkitAudioContext)();'
			+ '  if (ctx.state === "suspended") { try { ctx.resume(); } catch(_e){} }'
			+ '  var dest = ctx.createMediaStreamDestination();'
			+ '  var silence = ctx.createConstantSource();'
			+ '  silence.offset.value = 0;'
			+ '  var silenceGain = ctx.createGain();'
			+ '  silenceGain.gain.value = 0;'
			+ '  silence.connect(silenceGain).connect(dest);'
			+ '  try { silence.start(); } catch(_e){}'
			+ '  try { console.log("[vdotool speaker iframe] AudioContext + dest ready", "tracks=", dest.stream.getAudioTracks().length); } catch(_e){}'
			+ '  var __vtPlayingSource = null;'
			+ '  var __vtChain = Promise.resolve();'
			+ '  window.__vtPlay = function(buf, clipName){'
			+ '    __vtChain = __vtChain.then(function(){'
			+ '      return ctx.decodeAudioData(buf).then(function(audioBuf){'
			+ '        return new Promise(function(resolve){'
			+ '          try {'
			+ '            var src = ctx.createBufferSource();'
			+ '            src.buffer = audioBuf;'
			+ '            src.connect(dest);'
			+ '            __vtPlayingSource = src;'
			+ '            src.onended = function(){ __vtPlayingSource = null; resolve(); };'
			+ '            src.start();'
			+ '            try { console.log("[vdotool speaker iframe] playing clip", clipName, "duration=", audioBuf.duration.toFixed(2)+"s"); } catch(_e){}'
			+ '          } catch(e) { try{console.log("[vdotool speaker iframe] playBuffer error", e);}catch(_e){}; resolve(); }'
			+ '        });'
			+ '      }).catch(function(e){'
			+ '        try { console.log("[vdotool speaker iframe] decodeAudioData failed for", clipName, e && e.name, e && e.message); } catch(_e){}'
			+ '      });'
			+ '    });'
			+ '    return __vtChain;'
			+ '  };'
			+ '  window.__vtStop = function(){'
			+ '    if (__vtPlayingSource) { try { __vtPlayingSource.stop(); } catch(_e){} __vtPlayingSource = null; }'
			+ '    __vtChain = Promise.resolve();'
			+ '  };'
			+ '  try {'
			+ '    var origGum = navigator.mediaDevices && navigator.mediaDevices.getUserMedia'
			+ '      ? navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices)'
			+ '      : null;'
			+ '    if (navigator.mediaDevices) {'
			+ '      navigator.mediaDevices.getUserMedia = function(constraints){'
			+ '        try { console.log("[vdotool speaker iframe] gUM intercepted", JSON.stringify(constraints)); } catch(_e){}'
			+ '        if (constraints && constraints.video && origGum) { return origGum(constraints); }'
			+ '        return Promise.resolve(dest.stream);'
			+ '      };'
			+ '      try { console.log("[vdotool speaker iframe] gUM override installed"); } catch(_e){}'
			+ '    }'
			+ '  } catch(e) { try{console.log("[vdotool speaker iframe] override failed", e);}catch(_e){} }'
			+ '  try { window.parent.postMessage({type:"vdotool/iframe-ready"}, "*"); } catch(_e){}'
			+ '  setTimeout(function(){ location.replace(' + JSON.stringify(targetUrl) + '); }, 10);'
			+ '})();'
			+ '<\/script>'
			+ '</body></html>';
		return bootstrap;
	}

	function spawnPusherIframe(roomId, agentStreamId, sessionId, origin) {
		var iframe = document.createElement('iframe');
		iframe.style.width = '1px';
		iframe.style.height = '1px';
		iframe.style.position = 'fixed';
		iframe.style.left = '-1000px';
		iframe.style.top = '-1000px';
		iframe.style.opacity = '0';
		iframe.allow = 'autoplay; microphone; camera';
		iframe.srcdoc = buildIframeSrcdoc(roomId, agentStreamId, sessionId, origin);
		return iframe;
	}

	// ------------------------------------------------------------------
	// Audio-queue polling loop.
	// ------------------------------------------------------------------

	function speaker(sessionId, iframe) {
		// All audio playback happens inside the iframe (the
		// MediaStreamDestinationNode lives there so VDO.Ninja's
		// gUM-override can hand it directly to its pusher). The parent
		// only ferries clip bytes via iframe.contentWindow.__vtPlay.
		var playedClips = Object.create(null);
		var lastInterruptEpoch = 0;

		function fetchQueue() {
			var url = WRITER_PATH_QUEUE + '?sessionId=' + encodeURIComponent(sessionId);
			return fetch(url, { credentials: 'same-origin' })
				.then(function (r) {
					if (!r.ok) throw new Error('queue fetch HTTP ' + r.status);
					return r.json();
				});
		}

		function fetchClip(clipName) {
			var url = WRITER_PATH_CLIP + encodeURIComponent(sessionId) + '/' + encodeURIComponent(clipName);
			return fetch(url, { credentials: 'same-origin' })
				.then(function (r) {
					if (!r.ok) throw new Error('clip fetch HTTP ' + r.status);
					return r.arrayBuffer();
				});
		}

		function ackClip(clipName) {
			var url = WRITER_PATH_ACK + '?sessionId=' + encodeURIComponent(sessionId)
				+ '&clip=' + encodeURIComponent(clipName);
			return fetch(url, { method: 'POST', credentials: 'same-origin' })
				.catch(function (e) { log('ack failed', e); });
		}

		function iframePlay(buf, clipName) {
			// Same-origin iframe → cross-window function call works.
			// __vtPlay is installed by buildIframeSrcdoc above.
			try {
				var w = iframe.contentWindow;
				if (w && typeof w.__vtPlay === 'function') {
					return w.__vtPlay(buf, clipName);
				}
			} catch (e) {
				log('iframePlay failed', e);
			}
			return Promise.resolve();
		}

		function iframeStop() {
			try {
				var w = iframe.contentWindow;
				if (w && typeof w.__vtStop === 'function') {
					w.__vtStop();
				}
			} catch (_e) {}
		}

		var busy = false;
		async function tick() {
			if (busy) return;
			busy = true;
			try {
				var q = await fetchQueue();
				if (q && typeof q.interrupt_epoch_ms === 'number' && q.interrupt_epoch_ms > lastInterruptEpoch) {
					lastInterruptEpoch = q.interrupt_epoch_ms;
					log('interrupt epoch advanced -> aborting current clip');
					iframeStop();
				}
				var pending = (q && Array.isArray(q.pending)) ? q.pending : [];
				for (var i = 0; i < pending.length; i++) {
					var entry = pending[i];
					if (!entry || !entry.clip) continue;
					if (playedClips[entry.clip]) continue;
					playedClips[entry.clip] = true;
					log('handing clip to iframe', entry.clip, 'text=', JSON.stringify(entry.text || '').slice(0, 60));
					try {
						var bytes = await fetchClip(entry.clip);
						await iframePlay(bytes, entry.clip);
					} catch (e) {
						log('clip playback failed', entry.clip, e);
					}
					await ackClip(entry.clip);
				}
			} catch (e) {
				log('tick error', e);
			} finally {
				busy = false;
			}
		}
		return { tick: tick };
	}

	// ------------------------------------------------------------------
	// Public entry point.
	// ------------------------------------------------------------------

	globalScope.startVdotoolSpeaker = function startVdotoolSpeaker(sessionId) {
		if (!sessionIdValid(sessionId)) {
			log('invalid sessionId, speaker disabled');
			return;
		}

		fetch('/vdotool/healthz', { credentials: 'same-origin' })
			.then(function (r) { return r.json(); })
			.then(function (h) {
				if (!h || h.tts === false) {
					log('TTS disabled by writer (healthz says so); speaker not starting');
					return;
				}
				beginSpeaker(sessionId);
			})
			.catch(function (e) {
				log('healthz probe failed; starting speaker anyway', e);
				beginSpeaker(sessionId);
			});
	};

	function beginSpeaker(sessionId) {
		var params = new URLSearchParams(location.search);
		var roomId = params.get('room') || '';
		if (!roomId) {
			log('no room param in viewer URL; cannot publish audio');
			return;
		}
		var agentStreamId = 'vt_' + sessionId + '_agent';

		var iframe = spawnPusherIframe(roomId, agentStreamId, sessionId, location.origin);

		// Wait for the iframe to log iframe-ready (it'll already have
		// installed __vtPlay by then) before we start polling. The
		// iframe-ready notice fires BEFORE the iframe redirects to the
		// pusher URL — VDO.Ninja's gUM-override resolves to dest.stream
		// after the redirect. The parent doesn't need to do anything
		// at iframe-ready time anymore — __vtPlay is already callable.
		function onMsg(ev) {
			if (ev.source !== iframe.contentWindow) return;
			if (!ev.data || ev.data.type !== 'vdotool/iframe-ready') return;
			log('iframe ready; speaker can now play clips');
		}
		window.addEventListener('message', onMsg);

		document.body.appendChild(iframe);
		log('spawned pusher iframe; room=' + roomId + ' agent_stream=' + agentStreamId);

		var sp = speaker(sessionId, iframe);
		sp.tick();
		setInterval(sp.tick, POLL_INTERVAL_MS);
	}
})(window);
