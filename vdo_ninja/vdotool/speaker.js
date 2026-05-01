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
	// Synthetic audio: AudioContext + destination node from which we
	// publish a single MediaStreamTrack. TTS clips are decoded into
	// AudioBuffers and scheduled on this node sequentially.
	// ------------------------------------------------------------------

	function createSyntheticAudioStream() {
		var ctx = new (window.AudioContext || window.webkitAudioContext)();
		if (ctx.state === 'suspended') {
			ctx.resume().catch(function () {});
		}
		var dest = ctx.createMediaStreamDestination();
		// A persistent silent oscillator keeps the track live even when
		// no clip is playing. Without this, some browsers mark the track
		// as "ended" during idle and VDO.Ninja drops it.
		var silence = ctx.createConstantSource();
		silence.offset.value = 0;
		var gain = ctx.createGain();
		gain.gain.value = 0;
		silence.connect(gain).connect(dest);
		silence.start();
		return { ctx: ctx, dest: dest };
	}

	// ------------------------------------------------------------------
	// Companion pusher iframe.
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

		var bootstrap = ''
			+ '<!DOCTYPE html><html><head><meta charset="utf-8">'
			+ '<title>vdotool speaker</title></head><body>'
			+ '<script>'
			+ '(function(){'
			+ '  var __vtStreamResolver = null;'
			+ '  var __vtStreamPromise = new Promise(function(res){__vtStreamResolver = res;});'
			+ '  window.addEventListener("message", function(ev){'
			+ '    if (ev.source !== window.parent) return;'
			+ '    if (!ev.data || ev.data.type !== "vdotool/stream") return;'
			+ '    if (__vtStreamResolver && ev.data.stream) {'
			+ '      __vtStreamResolver(ev.data.stream);'
			+ '      __vtStreamResolver = null;'
			+ '      try { console.log("[vdotool speaker iframe] received stream from parent"); } catch(_e){}'
			+ '    }'
			+ '  });'
			+ '  try {'
			+ '    var origGum = navigator.mediaDevices && navigator.mediaDevices.getUserMedia'
			+ '      ? navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices)'
			+ '      : null;'
			+ '    if (navigator.mediaDevices) {'
			+ '      navigator.mediaDevices.getUserMedia = function(constraints){'
			+ '        try { console.log("[vdotool speaker iframe] gUM intercepted", constraints); } catch(_e){}'
			+ '        if (constraints && constraints.video && origGum) {'
			+ '          return origGum(constraints);'
			+ '        }'
			+ '        return __vtStreamPromise;'
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

	function speaker(sessionId, audioCtx, audioDest) {
		var playingSource = null;
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

		function playBuffer(buffer) {
			return new Promise(function (resolve) {
				try {
					var src = audioCtx.createBufferSource();
					src.buffer = buffer;
					src.connect(audioDest);
					playingSource = src;
					src.onended = function () {
						playingSource = null;
						resolve();
					};
					src.start();
				} catch (e) {
					log('playBuffer error', e);
					playingSource = null;
					resolve();
				}
			});
		}

		function stopCurrent() {
			if (playingSource) {
				try { playingSource.stop(); } catch (_e) {}
				playingSource = null;
			}
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
					stopCurrent();
				}
				var pending = (q && Array.isArray(q.pending)) ? q.pending : [];
				for (var i = 0; i < pending.length; i++) {
					var entry = pending[i];
					if (!entry || !entry.clip) continue;
					if (playedClips[entry.clip]) continue;
					playedClips[entry.clip] = true;
					log('playing clip', entry.clip, 'text=', JSON.stringify(entry.text || '').slice(0, 60));
					try {
						var bytes = await fetchClip(entry.clip);
						var buf = await audioCtx.decodeAudioData(bytes);
						await playBuffer(buf);
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
		var synth;
		try {
			synth = createSyntheticAudioStream();
		} catch (e) {
			log('could not create AudioContext', e);
			return;
		}
		var tracks = synth.dest.stream.getAudioTracks();
		log('synthetic audio track ready:', tracks[0] && tracks[0].label);

		var params = new URLSearchParams(location.search);
		var roomId = params.get('room') || '';
		if (!roomId) {
			log('no room param in viewer URL; cannot publish audio');
			return;
		}
		var agentStreamId = 'vt_' + sessionId + '_agent';

		var iframe = spawnPusherIframe(roomId, agentStreamId, sessionId, location.origin);

		function onMsg(ev) {
			if (ev.source !== iframe.contentWindow) return;
			if (!ev.data || ev.data.type !== 'vdotool/iframe-ready') return;
			try {
				iframe.contentWindow.postMessage(
					{ type: 'vdotool/stream', stream: synth.dest.stream },
					location.origin,
					[]
				);
				log('handed MediaStream to pusher iframe');
			} catch (e) {
				log('failed to post MediaStream to iframe', e);
			}
		}
		window.addEventListener('message', onMsg);

		document.body.appendChild(iframe);
		log('spawned pusher iframe; room=' + roomId + ' agent_stream=' + agentStreamId);

		var sp = speaker(sessionId, synth.ctx, synth.dest);
		sp.tick();
		setInterval(sp.tick, POLL_INTERVAL_MS);
	}
})(window);
