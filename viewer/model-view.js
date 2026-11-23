"use strict";
// Minimal WebGL2 viewer for the decoded model GLBs (Y-up, one mesh, primitives keyed by shaderIndex).
(function () {
  const VS = `#version 300 es
    layout(location=0) in vec3 pos; layout(location=1) in vec3 nrm; layout(location=2) in vec2 uv; layout(location=3) in vec4 col;
    layout(location=4) in vec2 uv1; layout(location=5) in vec4 blendW;
    layout(location=6) in vec4 aJ0; layout(location=7) in vec4 aW0;
    layout(location=8) in vec4 aJ1; layout(location=9) in vec4 aW1;
    uniform mat4 mvp, model; uniform bool flipV, useSkin, hasSkin8;
    uniform sampler2D boneTex;
    out vec3 vN; out vec3 vW; out vec3 vBindPos; out vec2 vUV; out vec2 vUV1; out vec4 vCol; out vec4 vBlend;
    mat4 fetchBone(int idx){
      return mat4(
        texelFetch(boneTex, ivec2(0, idx), 0),
        texelFetch(boneTex, ivec2(1, idx), 0),
        texelFetch(boneTex, ivec2(2, idx), 0),
        texelFetch(boneTex, ivec2(3, idx), 0)
      );
    }
    void main(){
      vec3 lp = pos;
      vec3 ln = nrm;
      if(useSkin){
        mat4 sm = fetchBone(int(aJ0.x + 0.5)) * aW0.x
                + fetchBone(int(aJ0.y + 0.5)) * aW0.y
                + fetchBone(int(aJ0.z + 0.5)) * aW0.z
                + fetchBone(int(aJ0.w + 0.5)) * aW0.w;
        if(hasSkin8){
          sm += fetchBone(int(aJ1.x + 0.5)) * aW1.x
              + fetchBone(int(aJ1.y + 0.5)) * aW1.y
              + fetchBone(int(aJ1.z + 0.5)) * aW1.z
              + fetchBone(int(aJ1.w + 0.5)) * aW1.w;
        }
        lp = (sm * vec4(pos, 1.0)).xyz;
        ln = mat3(sm) * nrm;
      }
      vBindPos = (model * vec4(pos, 1.0)).xyz;
      vec4 w=model*vec4(lp,1.); vW=w.xyz;
      vN=transpose(inverse(mat3(model)))*ln;
      vUV=flipV?vec2(uv.x,1.-uv.y):uv;
      vUV1=flipV?vec2(uv1.x,1.-uv1.y):uv1;
      vCol=col; vBlend=blendW;
      gl_Position=mvp*vec4(lp,1.);
    }`;
  const SHADOW_VS = `#version 300 es
    layout(location=0) in vec3 pos;
    layout(location=6) in vec4 aJ0; layout(location=7) in vec4 aW0;
    layout(location=8) in vec4 aJ1; layout(location=9) in vec4 aW1;
    uniform mat4 lightMVP;
    uniform bool useSkin, hasSkin8;
    uniform sampler2D boneTex;
    mat4 fetchBone(int idx){
      return mat4(
        texelFetch(boneTex, ivec2(0, idx), 0),
        texelFetch(boneTex, ivec2(1, idx), 0),
        texelFetch(boneTex, ivec2(2, idx), 0),
        texelFetch(boneTex, ivec2(3, idx), 0)
      );
    }
    void main(){
      vec3 lp = pos;
      if(useSkin){
        mat4 sm = fetchBone(int(aJ0.x + 0.5)) * aW0.x
                + fetchBone(int(aJ0.y + 0.5)) * aW0.y
                + fetchBone(int(aJ0.z + 0.5)) * aW0.z
                + fetchBone(int(aJ0.w + 0.5)) * aW0.w;
        if(hasSkin8){
          sm += fetchBone(int(aJ1.x + 0.5)) * aW1.x
              + fetchBone(int(aJ1.y + 0.5)) * aW1.y
              + fetchBone(int(aJ1.z + 0.5)) * aW1.z
              + fetchBone(int(aJ1.w + 0.5)) * aW1.w;
        }
        lp = (sm * vec4(pos, 1.0)).xyz;
      }
      gl_Position = lightMVP * vec4(lp, 1.0);
    }`;
  const SHADOW_FS = `#version 300 es
    precision highp float;
    void main(){}`;
  const SKEL_VS = `#version 300 es
    layout(location=0) in vec3 pos;
    layout(location=1) in vec3 col;
    layout(location=2) in float psize;
    uniform mat4 vp;
    out vec3 vC;
    void main(){
      vC = col;
      gl_Position = vp * vec4(pos, 1.0);
      gl_PointSize = psize;
    }`;
  const SKEL_FS = `#version 300 es
    precision highp float;
    in vec3 vC;
    uniform bool isPoint;
    out vec4 o;
    void main(){
      if(isPoint){
        vec2 d = gl_PointCoord * 2.0 - 1.0;
        float r2 = dot(d, d);
        if(r2 > 1.0) discard;
        float edge = smoothstep(1.0, 0.55, r2);
        o = vec4(mix(vC * 0.5, vC, edge), 0.96);
      } else {
        o = vec4(vC, 0.92);
      }
    }`;
  const GROOM_VS = `#version 300 es
    layout(location=0) in vec4 posT;
    uniform mat4 vp;
    uniform mat4 boneDelta;
    uniform vec3 standingOff;
    out float vT;
    out vec3 vW;
    void main(){
      vec3 lp = posT.xyz - standingOff;
      vec3 wp = (boneDelta * vec4(lp, 1.0)).xyz + standingOff;
      vT = posT.w;
      vW = wp;
      gl_Position = vp * vec4(wp, 1.0);
    }`;
  const GROOM_FS = `#version 300 es
    precision highp float;
    in float vT;
    in vec3 vW;
    uniform vec3 rootCol;
    uniform vec3 tipCol;
    uniform vec3 eye;
    uniform float alpha;
    uniform vec3 uKeyDir, uKeyCol, uFillDir, uFillCol, uRimDir, uRimCol, uAmbCol;
    uniform float uExposure;
    out vec4 o;
    float h31(vec3 p){
      p = fract(p * 0.1031);
      p += dot(p, p.yzx + 33.33);
      return fract((p.x + p.y) * p.z);
    }
    vec3 acesG(vec3 x){
      return clamp((x * (2.51 * x + 0.03)) / (x * (2.43 * x + 0.59) + 0.14), 0.0, 1.0);
    }
    void main(){
      float t = clamp(vT, 0.0, 1.0);
      vec3 baseSrgb = mix(rootCol, tipCol, smoothstep(0.05, 0.88, t));
      vec3 base = pow(max(baseSrgb, vec3(0.0)), vec3(2.2));
      float jitter = (h31(floor(vW * 260.0)) - 0.5) * 0.12;
      vec3 V = normalize(eye - vW);
      vec3 approxN = normalize(vec3(vW.x, max(0.18, (vW.y - 1.56) * 2.5), vW.z + 0.02));
      float ndl1 = max(dot(approxN, uKeyDir), 0.0);
      float ndl2 = max(dot(approxN, uFillDir), 0.0);
      float ndl3 = max(dot(approxN, uRimDir), 0.0);
      // Wrapped diffuse + Kajiya-Kay specular sheen + backlit rim under active studio rig
      vec3 diff = uAmbCol * 0.85
                + uKeyCol * (0.14 + 0.42 * ndl1)
                + uFillCol * (0.10 + 0.34 * ndl2)
                + uRimCol * (0.06 + 0.24 * ndl3);
      vec3 H1 = normalize(uKeyDir + V);
      float spec1 = pow(max(0.0, 1.0 - abs(dot(approxN, H1) - 0.80)), 14.0) * smoothstep(0.08, 0.55, t);
      vec3 H2 = normalize(uFillDir + V);
      float spec2 = pow(max(0.0, 1.0 - abs(dot(approxN, H2) - 0.80)), 10.0) * smoothstep(0.08, 0.55, t);
      float rim = pow(1.0 - max(dot(approxN, V), 0.0), 2.6) * (0.18 + 0.38 * t);
      float rootAo = mix(0.50, 1.0, smoothstep(0.0, 0.42, t));
      vec3 specTint = mix(pow(max(tipCol, vec3(0.0)), vec3(2.2)), vec3(1.0), 0.52);
      vec3 lin = base * (1.0 + jitter) * rootAo * diff
               + specTint * (uKeyCol * spec1 * 0.18 + uFillCol * spec2 * 0.10 + uRimCol * rim * 0.22);
      vec3 col = pow(acesG(lin * uExposure), vec3(1.0 / 2.2));
      o = vec4(clamp(col, 0.0, 1.0), alpha);
    }`;
  const BG_FS = `#version 300 es
    precision highp float;
    in vec2 vUV;
    uniform int bgMode; // 0 = studio cyclorama gradient, 1 = 3D environment dome, 2 = exact solid
    uniform vec3 bgTop, bgMid, bgBot;
    uniform float bgVignette, bgBright;
    uniform vec3 camRight, camUp, camFwd;
    uniform float aspect, tanHalfFov;
    uniform vec3 uEnvGround, uEnvHorizon, uEnvZenith;
    uniform vec3 uKeyDir, uKeyCol, uFillDir, uFillCol, uRimDir, uRimCol;
    uniform float uEnvStrip, uExposure;
    out vec4 o;
    vec3 acesB(vec3 x){
      return clamp((x * (2.51 * x + 0.03)) / (x * (2.43 * x + 0.59) + 0.14), 0.0, 1.0);
    }
    void main(){
      if (bgMode == 2) {
        o = vec4(clamp(bgMid, 0.0, 1.0), 1.0);
        return;
      }
      if (bgMode == 1) {
        vec2 ndc = vUV * 2.0 - 1.0;
        vec3 R = normalize(camFwd + ndc.x * aspect * tanHalfFov * camRight + ndc.y * tanHalfFov * camUp);
        float y = R.y;
        vec3 sky = y >= 0.0
          ? mix(uEnvHorizon, uEnvZenith, pow(clamp(y, 0.0, 1.0), 0.62))
          : mix(uEnvHorizon * 0.48, uEnvGround, clamp(-y * 2.6, 0.0, 1.0));
        // Softbox / sun / rim studio glow disks projected into the 3D environment dome
        float kGlow = pow(max(dot(R, uKeyDir), 0.0), 26.0) * 0.65 + pow(max(dot(R, uKeyDir), 0.0), 220.0) * 1.4;
        float fGlow = pow(max(dot(R, uFillDir), 0.0), 10.0) * 0.32;
        float rGlow = pow(max(dot(R, uRimDir), 0.0), 16.0) * 0.28;
        float strip = smoothstep(0.58, 0.90, y) * uEnvStrip * 0.42;
        float horizBand = exp(-abs(y) * 9.0) * 0.12;
        vec3 col = sky
          + (uKeyCol / 2.2) * kGlow
          + (uFillCol / 1.4) * fGlow
          + (uRimCol / 1.4) * rGlow
          + uEnvHorizon * horizBand
          + vec3(0.92, 0.95, 1.0) * strip;
        col = pow(acesB(col * bgBright * mix(0.85, 1.15, clamp((uExposure - 0.5) / 1.5, 0.0, 1.0))), vec3(1.0 / 2.2));
        o = vec4(col, 1.0);
        return;
      }
      // Mode 0: Photography studio cyclorama (3-stop vertical sweep + subtle radial center glow + edge vignette)
      float t = clamp(vUV.y, 0.0, 1.0);
      vec3 grad = t < 0.48
        ? mix(bgBot, bgMid, smoothstep(0.0, 0.48, t))
        : mix(bgMid, bgTop, smoothstep(0.48, 1.0, t));
      vec2 d = (vUV - vec2(0.5, 0.54)) * vec2(aspect * 0.72, 1.0);
      float rad = length(d);
      float spot = exp(-rad * rad * 2.15) * 0.12;
      float vig = 1.0 - smoothstep(0.38, 1.28, rad) * bgVignette;
      vec3 col = clamp((grad + bgMid * spot + vec3(spot * 0.25)) * vig * bgBright, 0.0, 1.0);
      o = vec4(col, 1.0);
    }`;
  const POST_VS = `#version 300 es
    out vec2 vUV;
    void main(){
      vec2 p = vec2((gl_VertexID == 1) ? 3.0 : -1.0, (gl_VertexID == 2) ? 3.0 : -1.0);
      vUV = p * 0.5 + 0.5;
      gl_Position = vec4(p, 0.0, 1.0);
    }`;
  const POST_FS = `#version 300 es
    precision highp float;
    in vec2 vUV;
    uniform sampler2D sceneTex, trailTex;
    uniform float pfTime, pfIntensity, pfBloom, pfLensK1, pfLensK2, pfChromK1, pfChromK2;
    uniform vec3 pfChromCol, pfVigCol;
    uniform float pfVigInt, pfVigIn, pfVigOut, pfVigBloom, pfDesat, pfShCon, pfHlCon, pfDtCon;
    uniform float pfNoise, pfExposure, pfTrail, pfRedShadow, pfUvParty;
    out vec4 o;

    float hash12(vec2 p){
      vec3 p3 = fract(vec3(p.xyx) * 0.1031);
      p3 += dot(p3, p3.yzx + 33.33);
      return fract((p3.x + p3.y) * p3.z);
    }
    vec2 distortUV(vec2 uv, float k1, float k2){
      vec2 d = uv * 2.0 - 1.0;
      float r2 = dot(d, d);
      float r = sqrt(r2);
      float f = 1.0 + k1 * r2 + k2 * r2 * r;
      // Auto-scaling keeps frame edges cleanly filled (lens_dist_use_auto_scaling)
      float edge = 1.0 + min(0.0, k1 + k2);
      return (d * f / max(edge, 0.45)) * 0.5 + 0.5;
    }
    void main(){
      float k1 = pfLensK1 * pfIntensity * 0.32;
      float k2 = pfLensK2 * pfIntensity * 0.95;
      vec2 uv = distortUV(vUV, k1, k2);
      vec2 cvec = uv - 0.5;
      float rad = length(cvec);
      vec2 cdir = rad > 1e-4 ? (cvec / rad) : vec2(0.0);

      // RAGE 9 DistortionWarp chromatic aberration with custom split_col_rgb spectrum
      float caAmt = (abs(pfChromK1) * 0.14 + abs(pfChromK2) * 0.12) * pfIntensity * (0.35 + 1.4 * rad);
      vec2 caOff = cdir * caAmt * sign(pfChromK1 + pfChromK2 + 1e-5);
      vec3 base = texture(sceneTex, clamp(uv, 0.001, 0.999)).rgb;
      if (caAmt > 0.0005) {
        vec3 sP = texture(sceneTex, clamp(uv + caOff, 0.001, 0.999)).rgb;
        vec3 sN = texture(sceneTex, clamp(uv - caOff, 0.001, 0.999)).rgb;
        vec3 splitW = (length(pfChromCol) > 0.02) ? normalize(pfChromCol + 0.001) : vec3(1.0, 0.25, 0.85);
        vec3 compW = clamp(vec3(1.0) - splitW * 0.65, 0.15, 1.0);
        base = mix(base, vec3(sP.r, base.g, sN.b), clamp(caAmt * 16.0, 0.0, 0.72));
        base += (sP - base) * splitW * clamp(caAmt * 18.0, 0.0, 0.75);
        base += (sN - base) * compW * clamp(caAmt * 9.0, 0.0, 0.35);
      }

      // Soft multi-tap bloom glow + detail contrast (postfx_intensity_bloom / dt_contrast)
      vec2 px = 1.0 / vec2(textureSize(sceneTex, 0));
      vec3 blur = (
        texture(sceneTex, clamp(uv + px * vec2( 3.0,  2.0), 0.001, 0.999)).rgb +
        texture(sceneTex, clamp(uv + px * vec2(-3.0,  2.0), 0.001, 0.999)).rgb +
        texture(sceneTex, clamp(uv + px * vec2( 3.0, -2.0), 0.001, 0.999)).rgb +
        texture(sceneTex, clamp(uv + px * vec2(-3.0, -2.0), 0.001, 0.999)).rgb +
        texture(sceneTex, clamp(uv + px * vec2( 7.0,  0.0), 0.001, 0.999)).rgb +
        texture(sceneTex, clamp(uv + px * vec2(-7.0,  0.0), 0.001, 0.999)).rgb +
        texture(sceneTex, clamp(uv + px * vec2( 0.0,  7.0), 0.001, 0.999)).rgb +
        texture(sceneTex, clamp(uv + px * vec2( 0.0, -7.0), 0.001, 0.999)).rgb
      ) * 0.125;
      vec3 hi = max(blur - vec3(0.28), vec3(0.0));
      float vigR = smoothstep(max(0.05, pfVigIn * 0.55), max(0.35, pfVigOut * 0.45), rad * 1.45);
      float bloomAmt = (pfBloom * 0.85 + pfVigBloom * vigR * 0.22) * pfIntensity;
      vec3 col = base + hi * bloomAmt;

      // Detail contrast sharpening (postfx_local_exposure_dt_contrast > 1.0)
      if (abs(pfDtCon - 1.0) > 0.02) {
        col = mix(col, col + (base - blur) * (pfDtCon - 1.0) * 0.95, pfIntensity);
      }
      // Local exposure shadow/highlight contrast & exposure lift
      float luma = dot(col, vec3(0.2126, 0.7152, 0.0722));
      float shMix = 1.0 - smoothstep(0.0, 0.48, luma);
      float hlMix = smoothstep(0.45, 0.95, luma);
      col *= mix(1.0, mix(1.0, pfShCon, shMix) * mix(1.0, pfHlCon, hlMix), pfIntensity * 0.75);
      col *= pow(2.0, pfExposure * pfIntensity * 0.28);

      // Red shadow grading (hellTrip) & UV Party Mode indigo cast (POSTFX_DRUG_coke_UV)
      if (pfRedShadow > 0.01) {
        col.r += shMix * pfRedShadow * pfIntensity * 0.42;
        col.gb *= (1.0 - shMix * pfRedShadow * pfIntensity * 0.45);
      }
      if (pfUvParty > 0.01) {
        col = mix(col, col * vec3(0.62, 0.54, 1.38) + vec3(0.06, 0.03, 0.16) * (1.0 - luma), pfUvParty * pfIntensity * 0.72);
      }

      // Desaturation / saturation boost (RAGE: 1.0 = neutral, <1 = desat, >1 = hyper-sat)
      float sat = mix(1.0, pfDesat, pfIntensity);
      float lum2 = dot(col, vec3(0.2126, 0.7152, 0.0722));
      col = mix(vec3(lum2), col, sat);

      // Vignette darkening/tinting
      if (pfVigInt > 0.005) {
        col = mix(col, pfVigCol, clamp(vigR * pfVigInt * pfIntensity, 0.0, 0.92));
      }
      // Film grain / adrenaline / concussion noise
      if (pfNoise > 0.01) {
        float g = (hash12(gl_FragCoord.xy + fract(pfTime * 17.3) * 193.0) - 0.5) * 0.18 * pfNoise * pfIntensity;
        col += vec3(g);
      }
      // ScreenTrail persistence (Cocaine / MKU)
      if (pfTrail > 0.01) {
        vec3 prev = texture(trailTex, vUV).rgb;
        col = mix(col, max(col, prev * 0.96), clamp(pfTrail * pfIntensity * 0.78, 0.0, 0.82));
      }
      o = vec4(clamp(col, 0.0, 1.0), 1.0);
    }`;
  const FS = `#version 300 es
    precision highp float;
    precision highp sampler2DArray;
    precision highp sampler2DShadow;
    in vec3 vN; in vec3 vW; in vec3 vBindPos; in vec2 vUV; in vec2 vUV1; in vec4 vCol; in vec4 vBlend;
    uniform int mode, texMode, matMode, passType;
    uniform vec3 color, pal0, pal1, pal2, eye, skinGain;
    uniform sampler2D tex, normTex, layer1Tex, layer2Tex, layer3Tex, maskTex, tintTex, tintPalTex;
    uniform sampler2D specTex, spec2Tex, dustTex, liveryTex;
    uniform sampler2DArray lutArrayTex;
    uniform sampler2DShadow shadowTex;
    uniform mat4 lightVP;
    uniform bool useShadow;
    uniform float shadowBias;
    uniform bool useTex, useNorm, useColor, hasNormals, alphaCut, highlight, useTintPal;
    uniform bool useSpec, useSpec2, useDust, useLivery, useLutArray;
    uniform int tintRow, tintRowLen, tintPalWidth, lutSlices;
    uniform bool hasUV1, hasBlendW, hasVertAlpha, hasL1, hasL2, hasL3, useMask, useBilinearMask, useTintTex, lumaAlpha;
    // Procedural Studio controls (Vehicles & Weapons)
    uniform vec3 vehPrimary, vehSecondary, vehPearlCol;
    uniform float vehPaintRow1, vehPaintRow2, vehPaintSlice, vehPearl, vehClearcoat, vehDirt, vehWear;
    uniform int vehTwoTone, vehInterior;
    uniform bool vehLutExact;
    uniform int wpnFinish;
    uniform float wpnWear, liveryScale;
    // RAGE 9 Character Weathering (WET_SKIN / WET_DUST_SKIN / WET_UNDERWATER_SKIN), SunTan, Blood, Wounds & Drug Lighting
    uniform bool charFxOn, useBloodDecal, bloodDecalTintable;
    uniform sampler2D bloodDecalTex;
    uniform float charTime, charWetness, charSoak, charWaterHeight, charDroplets, charEyeWetness;
    uniform float charDust, charDirtAmt, charDirtSkinStr, charDirtClothStr;
    uniform vec3 charDirtCol;
    uniform vec2 charDirtRough;
    uniform float charUnderwater, charWaterLine, charCaustics;
    uniform float charSunTan, charSunBurn, charFlush;
    uniform int charTanLines;
    uniform float charGlints, charPedLightMult, charPedFresnel, charUvParty;
    uniform float charBloodSoak;
    uniform int charWoundCount;
    uniform vec4 charWoundPos[8];
    uniform vec4 charWoundParam[8];
    // Studio Lighting & Environment IBL uniforms
    uniform vec3 uKeyDir, uKeyCol, uFillDir, uFillCol, uRimDir, uRimCol;
    uniform vec3 uEnvGround, uEnvHorizon, uEnvZenith, uAmbBot, uAmbTop;
    uniform float uExposure, uEnvStrip, uShadowMin;
    out vec4 o;

    float cHash31(vec3 p) {
      p = fract(p * 0.1031);
      p += dot(p, p.yzx + 33.33);
      return fract((p.x + p.y) * p.z);
    }
    float cNoise3(vec3 p) {
      vec3 i = floor(p), f = fract(p);
      f = f * f * (3.0 - 2.0 * f);
      return mix(
        mix(mix(cHash31(i), cHash31(i + vec3(1,0,0)), f.x),
            mix(cHash31(i + vec3(0,1,0)), cHash31(i + vec3(1,1,0)), f.x), f.y),
        mix(mix(cHash31(i + vec3(0,0,1)), cHash31(i + vec3(1,0,1)), f.x),
            mix(cHash31(i + vec3(0,1,1)), cHash31(i + vec3(1,1,1)), f.x), f.y), f.z);
    }
    float cFbm(vec3 p) {
      return cNoise3(p) * 0.55 + cNoise3(p * 2.13 + 3.7) * 0.30 + cNoise3(p * 4.41 + 9.2) * 0.15;
    }
    // Virtual swimwear/garment occlusion mask in standing space (matches CS_SunTanUpdate min(target, mask))
    float evalTanMask(vec3 bp, int mode) {
      if (mode <= 0) return 1.0;
      float ax = abs(bp.x), y = bp.y, z = bp.z;
      float cov = 0.0;
      if (mode == 1) { // Bikini / Swimwear tan lines
        float topBand = smoothstep(1.18, 1.21, y) * (1.0 - smoothstep(1.34, 1.37, y)) * (1.0 - smoothstep(0.18, 0.21, ax));
        float strap = smoothstep(1.30, 1.33, y) * (1.0 - smoothstep(1.48, 1.51, y)) * smoothstep(0.055, 0.070, ax) * (1.0 - smoothstep(0.095, 0.110, ax));
        float bot = smoothstep(0.84, 0.87, y) * (1.0 - smoothstep(1.01, 1.04, y)) * (1.0 - smoothstep(0.19, 0.22, ax));
        cov = max(max(topBand, strap), bot);
      } else if (mode == 2) { // Tank top + shorts
        float torso = smoothstep(0.95, 0.98, y) * (1.0 - smoothstep(1.44, 1.47, y)) * (1.0 - smoothstep(0.18, 0.21, ax));
        float shorts = smoothstep(0.68, 0.71, y) * (1.0 - smoothstep(1.00, 1.03, y));
        cov = max(torso, shorts);
      } else if (mode == 3) { // Shorts / swim trunks only
        cov = smoothstep(0.64, 0.67, y) * (1.0 - smoothstep(1.02, 1.05, y));
      } else if (mode == 4) { // T-shirt + pants (farmer's tan: face, forearms, lower legs exposed)
        float shirt = smoothstep(0.94, 0.97, y) * (1.0 - smoothstep(1.47, 1.50, y)) * (1.0 - smoothstep(0.32, 0.35, ax));
        float pants = smoothstep(0.16, 0.19, y) * (1.0 - smoothstep(1.01, 1.04, y));
        cov = max(shirt, pants);
      }
      return 1.0 - clamp(cov, 0.0, 1.0);
    }

    const float PI = 3.14159265359;

    vec3 toLinear(vec3 c) { return pow(max(c, vec3(0.0)), vec3(2.2)); }
    vec3 toSRGB(vec3 c) { return pow(max(c, vec3(0.0)), vec3(1.0 / 2.2)); }
    vec3 acesTone(vec3 x) {
      return clamp((x * (2.51 * x + 0.03)) / (x * (2.43 * x + 0.59) + 0.14), 0.0, 1.0);
    }

    float evalShadow(vec3 wPos, vec3 N) {
      if (!useShadow) return 1.0;
      vec4 sp = lightVP * vec4(wPos + N * shadowBias, 1.0);
      vec3 sc = sp.xyz / max(sp.w, 1e-5) * 0.5 + 0.5;
      if (sc.x <= 0.0 || sc.x >= 1.0 || sc.y <= 0.0 || sc.y >= 1.0 || sc.z <= 0.0 || sc.z >= 1.0) return 1.0;
      vec2 texel = vec2(1.0 / 2048.0);
      float sum = 0.0;
      for (int x = -1; x <= 1; x++) {
        for (int y = -1; y <= 1; y++) {
          sum += texture(shadowTex, vec3(sc.xy + vec2(float(x), float(y)) * texel * 1.35, sc.z - 0.0008));
        }
      }
      return mix(uShadowMin, 1.0, sum / 9.0);
    }

    vec3 perturbNormal(vec3 N, vec3 p, vec2 uv) {
      vec3 dp1 = dFdx(p), dp2 = dFdy(p);
      vec2 duv1 = dFdx(uv), duv2 = dFdy(uv);
      vec3 dp2perp = cross(dp2, N), dp1perp = cross(N, dp1);
      vec3 T = dp2perp * duv1.x + dp1perp * duv2.x;
      vec3 B = dp2perp * duv1.y + dp1perp * duv2.y;
      float invmax = max(dot(T, T), dot(B, B));
      if (invmax < 1e-10) return N;
      float s = inversesqrt(invmax);
      vec2 m = texture(normTex, uv).rg * 2.0 - 1.0;
      float mz = sqrt(max(1.0 - dot(m, m), 0.0));
      return normalize(T * (m.x * s) + B * (m.y * s) + N * mz);
    }

    // Procedural studio/outdoor IBL driven by active lighting rig & rotation.
    vec3 sampleEnv(vec3 R, float rough) {
      float y = R.y;
      vec3 sky = y >= 0.0 ? mix(uEnvHorizon, uEnvZenith, pow(clamp(y, 0.0, 1.0), 0.65))
                          : mix(uEnvHorizon * 0.45, uEnvGround, clamp(-y * 2.8, 0.0, 1.0));
      float sharp = mix(42.0, 2.5, rough * rough);
      float s1 = pow(max(dot(R, uKeyDir), 0.0), sharp) * mix(2.2, 0.24, rough);
      float s2 = pow(max(dot(R, uFillDir), 0.0), sharp * 0.35) * mix(0.65, 0.14, rough);
      float s3 = pow(max(dot(R, uRimDir), 0.0), sharp * 0.5) * mix(0.45, 0.08, rough);
      float strip = smoothstep(0.62, 0.92, y) * uEnvStrip * mix(0.36, 0.08, rough);
      vec3 keyTint = uKeyCol / 2.25;
      vec3 fillTint = uFillCol / 0.85;
      vec3 rimTint = uRimCol / 0.65;
      vec3 col = sky + keyTint * s1 + fillTint * s2 + rimTint * s3 + vec3(0.88, 0.93, 1.0) * strip;
      return mix(col, (uEnvHorizon + uEnvGround) * 0.45, rough * 0.22);
    }

    float D_GGX(float NdotH, float a2) {
      float d = (NdotH * NdotH) * (a2 - 1.0) + 1.0;
      return a2 / max(PI * d * d, 1e-5);
    }
    float G_Smith(float NdotV, float NdotL, float rough) {
      float k = (rough + 1.0);
      k = (k * k) * 0.125;
      float gv = NdotV / (NdotV * (1.0 - k) + k);
      float gl = NdotL / (NdotL * (1.0 - k) + k);
      return gv * gl;
    }
    vec3 F_Schlick(float cosT, vec3 F0) {
      return F0 + (1.0 - F0) * pow(clamp(1.0 - cosT, 0.0, 1.0), 5.0);
    }

    vec3 evalLight(vec3 N, vec3 V, vec3 L, vec3 lCol, vec3 baseLin, float rough, float metal, float cc, float ccRough) {
      float NdotL = max(dot(N, L), 0.0);
      if (NdotL <= 0.0) return vec3(0.0);
      float NdotV = max(dot(N, V), 0.001);
      vec3 H = normalize(L + V);
      float NdotH = max(dot(N, H), 0.0);
      float HdotV = max(dot(H, V), 0.0);
      vec3 F0 = mix(vec3(0.04), baseLin, metal);
      float a = max(rough * rough, 0.015);
      float a2 = a * a;
      vec3 F = F_Schlick(HdotV, F0);
      float D = D_GGX(NdotH, a2);
      float G = G_Smith(NdotV, NdotL, rough);
      vec3 spec = (D * G * F) / max(4.0 * NdotV * NdotL, 0.001);
      vec3 kd = (1.0 - F) * (1.0 - metal);
      vec3 outL = (kd * baseLin / PI + spec) * lCol * NdotL;
      if (cc > 0.01) {
        float ca = max(ccRough * ccRough, 0.008);
        float cD = D_GGX(NdotH, ca * ca);
        float cG = G_Smith(NdotV, NdotL, ccRough);
        float cF = 0.04 + 0.96 * pow(1.0 - HdotV, 5.0);
        outL += vec3((cD * cG * cF) / max(4.0 * NdotV * NdotL, 0.001)) * cc * lCol * NdotL;
      }
      return outL;
    }

    // Weapon finish palette by preset & discrete COLOR_0.g zone ID (0..63).
    void weaponZoneMaterial(int finish, int zone, float metalHint, out vec3 alb, out float met, out float rgh) {
      bool isMetal = metalHint > 0.24;
      bool isGrip = (zone == 2 || zone == 5 || zone == 6 || zone == 10);
      bool isAccent = (zone == 3 || zone == 7 || zone == 11);
      if (finish == 1) { // Blued Steel & Walnut
        if (isMetal && !isGrip) { alb = vec3(0.14, 0.18, 0.24); met = 0.88; rgh = 0.28; }
        else if (isAccent)      { alb = vec3(0.68, 0.56, 0.32); met = 0.90; rgh = 0.26; }
        else                    { alb = vec3(0.32, 0.18, 0.10); met = 0.04; rgh = 0.46; }
      } else if (finish == 2) { // Coyote Tan / FDE
        if (isGrip)             { alb = vec3(0.14, 0.14, 0.15); met = 0.04; rgh = 0.72; }
        else if (isMetal)       { alb = vec3(0.36, 0.30, 0.23); met = 0.72; rgh = 0.38; }
        else                    { alb = vec3(0.55, 0.46, 0.34); met = 0.08; rgh = 0.58; }
      } else if (finish == 3) { // Olive Drab Military
        if (isGrip)             { alb = vec3(0.12, 0.13, 0.13); met = 0.04; rgh = 0.74; }
        else if (isMetal)       { alb = vec3(0.22, 0.24, 0.25); met = 0.78; rgh = 0.40; }
        else                    { alb = vec3(0.28, 0.33, 0.24); met = 0.10; rgh = 0.58; }
      } else if (finish == 4) { // Brushed Stainless / Chrome
        if (isGrip)             { alb = vec3(0.11, 0.12, 0.13); met = 0.05; rgh = 0.70; }
        else if (isMetal || isAccent) { alb = vec3(0.74, 0.77, 0.80); met = 0.94; rgh = 0.22; }
        else                    { alb = vec3(0.24, 0.26, 0.28); met = 0.22; rgh = 0.52; }
      } else if (finish == 5) { // Vice Gold / Luxury
        if (isGrip)             { alb = vec3(0.12, 0.09, 0.08); met = 0.06; rgh = 0.55; }
        else if (isMetal || isAccent) { alb = vec3(0.86, 0.67, 0.28); met = 0.95; rgh = 0.20; }
        else                    { alb = vec3(0.18, 0.16, 0.15); met = 0.35; rgh = 0.42; }
      } else if (finish == 6) { // Arctic Two-Tone
        if (isGrip)             { alb = vec3(0.14, 0.15, 0.17); met = 0.05; rgh = 0.68; }
        else if (isMetal)       { alb = vec3(0.26, 0.28, 0.31); met = 0.82; rgh = 0.34; }
        else                    { alb = vec3(0.76, 0.79, 0.82); met = 0.12; rgh = 0.46; }
      } else { // 0: Factory Tactical (authentic per-zone metal/polymer/rubber differentiation)
        if (zone == 0)          { alb = vec3(0.20, 0.22, 0.24); met = isMetal ? 0.80 : 0.18; rgh = isMetal ? 0.36 : 0.58; }
        else if (zone == 1)     { alb = vec3(0.27, 0.29, 0.32); met = isMetal ? 0.84 : 0.22; rgh = isMetal ? 0.32 : 0.54; }
        else if (zone == 2)     { alb = vec3(0.13, 0.14, 0.15); met = 0.05; rgh = 0.72; }
        else if (zone == 3)     { alb = vec3(0.42, 0.45, 0.48); met = 0.88; rgh = 0.28; }
        else if (zone == 4)     { alb = vec3(0.17, 0.18, 0.20); met = isMetal ? 0.74 : 0.12; rgh = 0.48; }
        else if (zone == 5)     { alb = vec3(0.11, 0.12, 0.13); met = 0.04; rgh = 0.76; }
        else if (zone == 7)     { alb = vec3(0.32, 0.36, 0.42); met = 0.85; rgh = 0.22; }
        else                    { alb = isMetal ? vec3(0.24, 0.26, 0.29) : vec3(0.18, 0.19, 0.21); met = isMetal ? 0.82 : 0.14; rgh = isMetal ? 0.36 : 0.60; }
      }
    }

    // Maps GTA VI's 9 chroma-key interior slots (LUT rows 40..48) to realistic interior palettes.
    vec3 vehicleInteriorColor(int theme, float row) {
      int k = int(clamp(row - 40.0, 0.0, 8.0));
      vec3 baseDash, seatCol, trimCol, carpetCol, accentCol;
      if (theme == 1) { // Slate Grey
        baseDash = vec3(0.20, 0.21, 0.23); seatCol = vec3(0.26, 0.27, 0.29);
        trimCol = vec3(0.32, 0.33, 0.36); carpetCol = vec3(0.14, 0.15, 0.16); accentCol = vec3(0.46, 0.48, 0.50);
      } else if (theme == 2) { // Saddle Tan
        baseDash = vec3(0.18, 0.14, 0.11); seatCol = vec3(0.42, 0.27, 0.16);
        trimCol = vec3(0.34, 0.22, 0.13); carpetCol = vec3(0.15, 0.12, 0.10); accentCol = vec3(0.54, 0.38, 0.24);
      } else if (theme == 3) { // Cream Leather
        baseDash = vec3(0.22, 0.20, 0.18); seatCol = vec3(0.72, 0.67, 0.58);
        trimCol = vec3(0.62, 0.58, 0.50); carpetCol = vec3(0.26, 0.24, 0.21); accentCol = vec3(0.80, 0.76, 0.68);
      } else if (theme == 4) { // Oxblood Red
        baseDash = vec3(0.14, 0.12, 0.13); seatCol = vec3(0.36, 0.10, 0.12);
        trimCol = vec3(0.26, 0.09, 0.11); carpetCol = vec3(0.12, 0.10, 0.11); accentCol = vec3(0.48, 0.14, 0.16);
      } else { // 0: Charcoal Black (factory default)
        baseDash = vec3(0.11, 0.115, 0.125); seatCol = vec3(0.15, 0.155, 0.165);
        trimCol = vec3(0.19, 0.20, 0.21); carpetCol = vec3(0.085, 0.09, 0.095); accentCol = vec3(0.28, 0.29, 0.31);
      }
      if (k == 0 || k == 1) return baseDash;
      if (k == 2 || k == 3) return seatCol;
      if (k == 4 || k == 5) return carpetCol;
      if (k == 6 || k == 7) return trimCol;
      return accentCol;
    }

    void main(){
      if(mode==9){ o=vec4(color,1.); return; }
      vec3 gN = hasNormals ? normalize(vN) : normalize(cross(dFdx(vW), dFdy(vW)));
      vec3 v = normalize(eye - vW);
      if(dot(gN,v)<0.) gN=-gN;
      vec2 nUV = (texMode == 5 && hasUV1) ? vUV1 : vUV;
      vec3 n = useNorm ? perturbNormal(gN, vW, nUV) : gN;
      vec3 l1 = uKeyDir;
      vec3 l2 = uFillDir;
      vec3 h = normalize(l1 + v);

      if(matMode == 2){
        float fres = pow(1. - max(dot(n, v), 0.), 4.);
        vec3 R = reflect(-v, n);
        vec3 env = sampleEnv(R, 0.06);
        vec3 gcol = mix(vec3(.04, .07, .09), env * 0.95, clamp(0.18 + 0.82 * fres, 0.0, 1.0));
        if(highlight) gcol = mix(gcol, vec3(.45, .92, .72), .45);
        o = vec4(toSRGB(acesTone(gcol * uExposure)), clamp(.22 + .52 * fres, .22, .84));
        return;
      }
      if(passType == 4 && !useTex){
        float fres = pow(1. - max(dot(n, v), 0.), 3.5);
        vec3 env = sampleEnv(reflect(-v, n), 0.08);
        vec3 wcol = mix(vec3(.02, .10, .15), env * vec3(0.75, 0.92, 0.98), clamp(0.22 + 0.78 * fres, 0.0, 1.0));
        if(highlight) wcol = mix(wcol, vec3(.45, .92, .72), .45);
        o = vec4(toSRGB(acesTone(wcol * uExposure)), clamp(.46 + .38 * fres, .42, .88));
        return;
      }

      vec3 base = color;
      float a = 1.0;
      float rough = 0.56;
      float metal = 0.04;
      float ao = 1.0;
      float clearcoat = (matMode == 1) ? 0.85 : 0.0;
      float ccRough = 0.08;
      vec3 pearlTint = vec3(0.0);

      if(texMode == 6){
        // GTA VI standard_vehicle_2lyr* procedural LUT + 2-layer PBR pipeline
        vec4 t = useTex ? texture(tex, vUV) : vec4(0.5, 0.5, 0.0, 1.0);
        vec4 matA = useSpec ? texture(specTex, vUV) : vec4(0.32, 0.35, 0.96, 1.0);
        a = useTex ? t.a : 1.0;
        float row = floor(vCol.a * 255.0 + 0.5);
        float rampX = useTex ? clamp(t.g, 0.12, 0.92) : 0.50;
        bool hasMultiLut = useLutArray && lutSlices >= 2;
        float pSlice = hasMultiLut ? clamp(vehPaintSlice, 1.0, float(lutSlices - 1)) : 0.0;

        // Sample primary & secondary automotive paints from GTA VI's 256-color paint catalog (Slices 1/2/5)
        vec3 paint1 = hasMultiLut
          ? texture(lutArrayTex, vec3(rampX, (vehPaintRow1 + 0.5) / 256.0, pSlice)).rgb
          : vehPrimary * (0.65 + 0.65 * rampX);
        vec3 paint2 = hasMultiLut
          ? texture(lutArrayTex, vec3(rampX, (vehPaintRow2 + 0.5) / 256.0, pSlice)).rgb
          : vehSecondary * (0.65 + 0.65 * rampX);

        // Classify GTA VI COLOR_0.a row zones (verified across impaler5/6, aircraft, wheels & tyres):
        //   0,1,2,3,5,61..63 = primary exterior panels (hood, bumpers, trunk, fenders, rear quarters)
        //   4                = roof
        //   59,60,64         = replaceable doors (FL, FR, RR)
        //   65..72           = mirrors / body-colored caps
        //   6..15            = lower body / secondary accent trim
        //   16..32           = dark satin chassis / wheel hub / underbody metal
        //   33..39           = tyre rubber (row 35 on all GTA VI tyres)
        //   40..48           = 9 chroma-key interior trim/seat/dash/carpet slots
        //   49..58           = chrome/silver badges, emblems & cutout trim (row 54 = IMPALER badge)
        //   73..78           = dark satin body side moldings
        //   79..236,241..255 = real fixed physical LUT materials in Slice 0 (grille, lights, rubber, chrome, brake iron)
        //   237..240         = wheel rim alloy lip/barrel slots
        bool isRoof = (row == 4.0);
        bool isDoors = (row == 59.0 || row == 60.0 || row == 64.0);
        bool isMirrors = (row >= 65.0 && row <= 72.0);
        bool isPrimaryPanel = (row <= 3.0 || row == 5.0 || (row >= 61.0 && row <= 63.0));
        bool isLowerTrim = (row >= 6.0 && row <= 15.0);
        bool isChassisMetal = (row >= 16.0 && row <= 32.0);
        bool isTyreRubber = (row >= 33.0 && row <= 39.0);
        bool isInterior = (row >= 40.0 && row <= 48.0);
        bool isBadgeChrome = (row >= 49.0 && row <= 58.0) || row == 117.0;
        bool isMolding = (row >= 73.0 && row <= 78.0);
        bool isRimAlloy = (row >= 237.0 && row <= 240.0);
        bool isBodyPaint = isPrimaryPanel || isRoof || isDoors || isMirrors;

        vec3 lutCol = color;
        if(vehLutExact && useLutArray && lutSlices >= 1){
          float sl = (row <= 32.0 && lutSlices >= 2) ? pSlice : 0.0;
          lutCol = texture(lutArrayTex, vec3(rampX, (row + 0.5) / 256.0, sl)).rgb;
        } else if(isBodyPaint){
          bool useSec = (vehTwoTone == 1 && (isRoof || isDoors)) || (vehTwoTone == 2 && (isRoof || isMirrors));
          lutCol = useSec ? paint2 : paint1;
        } else if(isLowerTrim){
          lutCol = (vehTwoTone == 2) ? paint2 : mix(paint1, vec3(0.13, 0.135, 0.145), 0.72);
        } else if(isChassisMetal){
          lutCol = vec3(0.22, 0.23, 0.25) * (0.72 + 0.56 * rampX);
        } else if(isTyreRubber){
          lutCol = vec3(0.085, 0.088, 0.094) * (0.68 + 0.64 * rampX);
        } else if(isInterior){
          lutCol = vehicleInteriorColor(vehInterior, row) * (0.75 + 0.50 * rampX);
        } else if(isBadgeChrome || isRimAlloy){
          lutCol = vec3(0.76, 0.78, 0.81) * (0.76 + 0.44 * rampX);
        } else if(isMolding){
          lutCol = vec3(0.14, 0.145, 0.155) * (0.74 + 0.52 * rampX);
        } else if(useLutArray && lutSlices >= 1){
          // Real fixed physical vehicle LUT swatch in Slice 0 (with Slice 3 weathered blend)
          float rowV = (row + 0.5) / 256.0;
          vec3 s0 = texture(lutArrayTex, vec3(rampX, rowV, 0.0)).rgb;
          if(lutSlices >= 4 && vehWear > 0.01){
            vec3 s3 = texture(lutArrayTex, vec3(rampX, rowV, 3.0)).rgb;
            s0 = mix(s0, s3, vehWear * 0.85);
          }
          lutCol = s0;
        } else {
          lutCol = color * (0.60 + 0.75 * rampX);
        }

        // Detail modulation from lyr0diffusetex.r (centered at 0.5) + cavity AO from lyr0materialatex.b
        float detail = useTex ? clamp(0.68 + 0.54 * t.r, 0.42, 1.24) : 1.0;
        base = clamp(lutCol * detail, 0.0, 1.0);
        ao = useSpec ? mix(0.45, 1.0, matA.b) : 1.0;
        if(vCol.r > 0.02 && vCol.r < 0.99) ao *= mix(0.76, 1.0, vCol.r);

        // Physically plausible metalness / roughness / clearcoat per semantic vehicle zone
        float luma = dot(lutCol, vec3(0.299, 0.587, 0.114));
        float sat = max(lutCol.r, max(lutCol.g, lutCol.b)) - min(lutCol.r, min(lutCol.g, lutCol.b));
        if(isBodyPaint){
          // Automotive basecoat + glossy clearcoat (lower metal on dark paints preserves rich black depth)
          metal = clamp(0.16 + 0.36 * matA.g + 0.12 * t.b, 0.08, 0.66);
          rough = clamp(mix(0.16, 0.42, matA.r) + vehWear * 0.20, 0.12, 0.78);
          clearcoat = vehClearcoat;
          ccRough = mix(0.04, 0.22, vehWear);
          pearlTint = vehPearlCol * (vehPearl * 0.48);
        } else if(isBadgeChrome || isRimAlloy || (!vehLutExact && luma > 0.60 && sat < 0.10)){
          // Chrome, badges, brushed aluminum & polished wheel alloy
          metal = 0.90;
          rough = clamp(mix(0.14, 0.32, matA.r), 0.10, 0.42);
          clearcoat = 0.30;
          ccRough = 0.06;
        } else if(isTyreRubber){
          // Vulcanized rubber tread & sidewalls
          metal = 0.02;
          rough = clamp(mix(0.68, 0.88, matA.r), 0.62, 0.92);
          clearcoat = 0.0;
        } else if(isInterior){
          // Leather, vinyl, fabric & carpet interior trim
          metal = 0.03;
          rough = clamp(mix(0.56, 0.82, matA.r), 0.48, 0.90);
          clearcoat = 0.0;
        } else if(isChassisMetal){
          // Dark satin steel, suspension, exhaust & wheel lug hardware
          metal = 0.68;
          rough = clamp(mix(0.28, 0.52, matA.r), 0.22, 0.62);
          clearcoat = 0.0;
        } else if(isLowerTrim || isMolding){
          // Satin body molding & bumper trim
          metal = 0.16;
          rough = clamp(mix(0.36, 0.58, matA.r), 0.28, 0.70);
          clearcoat = vehClearcoat * 0.25;
        } else if(sat > 0.42){
          // Tail / indicator lenses (rows 119..126)
          metal = 0.10;
          rough = 0.14;
          clearcoat = 0.88;
          ccRough = 0.05;
        } else {
          // Fixed LUT plastics, rubber seals, grille gunmetal, leather & iron
          metal = clamp(matA.g * 0.42, 0.02, 0.52);
          rough = clamp(mix(0.40, 0.78, matA.r), 0.30, 0.88);
          clearcoat = 0.0;
        }

        // Dirt & dust overlay from dusttexture (fb03ae36)
        if(vehDirt > 0.01){
          float dMask = useDust ? texture(dustTex, vUV * 2.5).g : 0.65;
          float dAmt = clamp(vehDirt * (0.45 + 0.85 * dMask) * (0.65 + 0.35 * max(-n.y, 0.0)), 0.0, 0.85);
          base = mix(base, vec3(0.32, 0.28, 0.23), dAmt);
          rough = mix(rough, 0.90, dAmt);
          metal = mix(metal, 0.02, dAmt);
          clearcoat *= (1.0 - dAmt);
        }
      } else if(texMode == 7){
        // GTA VI weapon / weapon_muv_alpha procedural PBR + zone IDs + live livery pipeline
        vec4 sp1 = useSpec ? texture(specTex, vUV) : (useTex ? texture(tex, vUV) : vec4(0.35, 0.45, 0.5, 1.0));
        vec4 sp2 = useSpec2 ? texture(spec2Tex, vUV) : vec4(0.05, 0.0, 0.55, 1.0);
        int zone = int(floor(vCol.g * 64.0 + 0.01));
        vec3 wAlb; float wMet; float wRgh;
        weaponZoneMaterial(wpnFinish, zone, sp1.r, wAlb, wMet, wRgh);
        // Micro-detail modulation from speculartex.b (centered around 0.5)
        float micro = clamp(0.75 + 0.50 * sp1.b, 0.45, 1.25);
        base = clamp(wAlb * micro, 0.0, 1.0);
        metal = clamp(mix(wMet, sp1.r, 0.45), 0.02, 0.96);
        rough = clamp(mix(wRgh, 1.0 - sp1.g, 0.55), 0.12, 0.90);
        ao = useSpec2 ? clamp(sp2.b * 1.45, 0.32, 1.0) : 1.0;
        // Optional live GTA VI weapon_liveries.ttpf camo/livery overlay
        if(useLivery){
          vec3 liv = texture(liveryTex, vUV * max(liveryScale, 0.25)).rgb;
          float livMask = clamp(1.0 - smoothstep(0.62, 0.90, sp1.r) * 0.45, 0.35, 1.0);
          base = mix(base, liv * micro, livMask * 0.92);
          metal = mix(metal, 0.22, livMask * 0.65);
          rough = mix(rough, 0.46, livMask * 0.50);
        }
        // Edge wear / scratches from speculartex2.r exposing bare worn steel
        float wear = clamp(sp2.r * wpnWear * 1.35, 0.0, 1.0);
        if(wear > 0.01){
          base = mix(base, vec3(0.68, 0.71, 0.75), wear * 0.85);
          metal = mix(metal, 0.92, wear);
          rough = mix(rough, 0.26, wear);
        }
      } else if(useTex){
        if(texMode == 5){
          vec2 dUV = hasUV1 ? vUV1 : (useMask ? vUV * 8.0 : vUV);
          vec4 t0 = texture(tex, dUV);
          vec3 mw = useMask ? texture(maskTex, vUV).rgb : vec3(0.0);
          vec3 c = t0.rgb;
          if(useMask && useBilinearMask && hasL1){
            vec3 c1 = texture(layer1Tex, dUV).rgb;
            vec3 c2 = hasL2 ? texture(layer2Tex, dUV).rgb : c;
            vec3 c3 = hasL3 ? texture(layer3Tex, dUV).rgb : c2;
            vec3 nearC = mix(c, c1, clamp(mw.b, 0.0, 1.0));
            vec3 farC = mix(c2, c3, clamp(mw.b, 0.0, 1.0));
            c = mix(nearC, farC, clamp(mw.g, 0.0, 1.0));
          } else {
            vec3 w = useMask ? (hasBlendW ? max(mw, vBlend.rgb * 0.65) : mw)
                             : (hasBlendW ? vBlend.rgb : vec3(0.32, 0.16, 0.0));
            if(hasL1) c = mix(c, texture(layer1Tex, dUV).rgb, clamp(w.r, 0.0, 1.0));
            if(hasL2) c = mix(c, texture(layer2Tex, dUV).rgb, clamp(w.g, 0.0, 1.0));
            if(hasL3) c = mix(c, texture(layer3Tex, dUV).rgb, clamp(w.b, 0.0, 1.0));
          }
          if(useTintTex) c = clamp(c * (0.55 + 0.75 * texture(tintTex, vUV).rgb), 0.0, 1.0);
          base = c * color;
          a = t0.a;
        } else {
          vec4 t = texture(tex, vUV);
          if(texMode == 1){ base = color * (0.38 + 0.72 * t.g); }
          else if(texMode == 2){
            vec3 c = mix(pal0, pal1, t.r);
            c = mix(c, pal2, t.b);
            base = clamp(c * clamp(t.g * 2.15, 0.12, 1.55), 0.0, 1.0);
          } else if(texMode == 3){
            float pore = clamp(t.g * 0.65 + t.r * 0.35, 0.0, 1.0);
            base = clamp(color * mix(0.76, 1.04, pore) + (t.r - t.g) * vec3(0.055, -0.015, -0.025), 0.0, 1.0);
            rough = 0.52;
          } else if(texMode == 4){
            base = mix(vec3(0.92, 0.90, 0.89), t.rgb, t.a);
            rough = 0.12; clearcoat = 0.9;
          } else if(texMode == 8){
            base = color;
            a = smoothstep(0.36, 0.52, t.r);
          } else {
            base = t.rgb * color;
            a = lumaAlpha ? clamp(max(t.r, max(t.g, t.b)) * 1.35, 0.0, 1.0) : t.a;
          }
        }
        // Standard PBR speculartex (c6f46e79) integration across props, animals, vegetation, etc.
        if(useSpec && texMode != 3 && texMode != 4){
          vec4 sp = texture(specTex, (texMode == 5 && hasUV1) ? vUV1 : vUV);
          rough = clamp(1.0 - sp.g * 0.82, 0.12, 0.94);
          metal = (matMode == 3) ? 0.0 : smoothstep(0.48, 0.88, sp.r) * 0.88;
        }
      } else if(useColor){
        base = vCol.rgb;
      }
      // Human skin PBR (ped_standard_head 379efcef / ped_skin 210fbc6a): soft natural skin sheen + non-metallic base.
      if(matMode == 3){
        base = clamp(base * skinGain, 0.0, 1.0);
        rough = useSpec ? mix(0.46, 0.68, rough) : 0.57;
        metal = 0.0;
        clearcoat = 0.06;
        ccRough = 0.44;
      }
      if(useTintPal && tintPalWidth > 0){
        int pidx = int(clamp(floor(vCol.b * 255.0 + 0.5), 0.0, 255.0));
        int pentry = tintRow * max(tintRowLen, 1) + pidx;
        if(pentry < 0 || pentry >= tintPalWidth) pentry = clamp(pidx, 0, tintPalWidth - 1);
        base *= texelFetch(tintPalTex, ivec2(pentry, 0), 0).rgb;
      }
      float va = hasVertAlpha ? vCol.a : 1.0;
      float effA = a * va;
      if((alphaCut || passType == 1) && useTex && effA < 0.4) discard;
      if(passType >= 2 && effA < 0.02) discard;

      // --- RAGE 9 Character Surface Effects (SunTan, Flush, WET_SKIN, WET_DUST_SKIN, Dirt, Blood, Wounds, Underwater) ---
      float fxWetEff = 0.0;
      float fxUwEff = 0.0;
      float fxCausticBoost = 0.0;
      vec3 sssTint = vec3(1.0, 0.34, 0.20);
      if (charFxOn) {
        bool isSkin = (matMode == 3);
        bool isEye = (texMode == 4);

        // 1. SunTan (ped_skin_processing CS_SunTanUpdate signed [-1,+1] melanin shift + sunburn erythema + tan lines)
        if (isSkin && (abs(charSunTan) > 0.005 || charSunBurn > 0.005)) {
          float tMask = evalTanMask(vBindPos, charTanLines);
          float tanVal = charSunTan * tMask;
          if (tanVal > 0.0) {
            // Melanin darkening & golden-bronze warm chroma shift
            vec3 bronze = pow(max(base, vec3(0.001)), vec3(1.0 + tanVal * 0.52, 1.0 + tanVal * 0.72, 1.0 + tanVal * 1.05));
            base = mix(base, bronze * vec3(1.02, 0.94, 0.84), clamp(tanVal * 0.88, 0.0, 0.92));
          } else if (tanVal < 0.0) {
            float pAmt = -tanVal;
            float lum = dot(base, vec3(0.2125, 0.7154, 0.0721));
            base = mix(base, clamp(mix(vec3(lum), base, 0.72) * 1.18 + vec3(0.04, 0.03, 0.04), 0.0, 1.0), pAmt * 0.75);
          }
          if (charSunBurn > 0.005) {
            float bAmt = charSunBurn * tMask * (0.65 + 0.35 * max(n.y, 0.0));
            base = mix(base, base * vec3(1.12, 0.56, 0.46) + vec3(0.14, 0.01, 0.0), clamp(bAmt * 0.72, 0.0, 0.78));
            sssTint = mix(sssTint, vec3(1.0, 0.18, 0.12), bAmt * 0.6);
            rough = mix(rough, 0.38, bAmt * 0.35);
          }
        }

        // 2. Vascular Flush (bloodflowmask: cheeks, nose, neck, upper chest)
        if (isSkin && charFlush > 0.005) {
          float faceZone = smoothstep(1.48, 1.58, vBindPos.y) * (1.0 - smoothstep(1.66, 1.72, vBindPos.y)) * smoothstep(-0.02, 0.08, vBindPos.z);
          float chestZone = smoothstep(1.18, 1.32, vBindPos.y) * (1.0 - smoothstep(1.46, 1.54, vBindPos.y));
          float fMask = clamp(faceZone * 1.15 + chestZone * 0.65 + 0.18, 0.0, 1.0) * charFlush;
          base = mix(base, base * vec3(1.10, 0.74, 0.70) + vec3(0.08, 0.0, 0.0), fMask * 0.58);
          sssTint = mix(sssTint, vec3(1.0, 0.20, 0.16), fMask * 0.5);
        }

        // 3. Dust & Weathering Dirt Definitions (WET_DUST_SKIN smoothstep(0.1,0.4) + 0000000ab000.xml DirtDefinitions)
        float nGrain = cFbm(vBindPos * 14.0);
        if (charDust > 0.005) {
          float upMask = clamp((max(n.y, 0.0) * 0.72 + 0.28 - 0.10) * 3.33333, 0.0, 1.0);
          float dAmt = clamp(charDust * upMask * (0.45 + 0.85 * nGrain), 0.0, 0.88);
          vec3 dustCol = mix(vec3(0.62, 0.57, 0.49), vec3(0.32, 0.27, 0.22), charWetness * 0.7);
          base = mix(base, dustCol, dAmt * (isSkin ? 0.68 : 0.82));
          rough = mix(rough, mix(0.90, 0.42, charWetness), dAmt);
          clearcoat *= (1.0 - dAmt * (1.0 - charWetness * 0.5));
        }
        if (charDirtAmt > 0.005) {
          float limbBias = clamp(1.0 - smoothstep(0.15, 1.55, vBindPos.y) * 0.45, 0.45, 1.0);
          float splotch = smoothstep(0.26, 0.78, cFbm(vBindPos * 7.5 + vec3(1.3, 0.0, 2.7)) + (charDirtAmt - 0.5) * 0.65);
          float str = isSkin ? charDirtSkinStr : charDirtClothStr;
          float dAmt = clamp(charDirtAmt * splotch * limbBias * max(str, 0.35), 0.0, 0.92);
          base = mix(base, charDirtCol * (0.75 + 0.45 * nGrain), dAmt);
          float dRough = mix(charDirtRough.x, charDirtRough.y, nGrain);
          rough = mix(rough, dRough, dAmt);
          if (charDirtRough.x < 0.32) {
            clearcoat = max(clearcoat, dAmt * 0.65);
            ccRough = mix(ccRough, charDirtRough.x, dAmt);
          } else {
            clearcoat *= (1.0 - dAmt);
          }
        }

        // 4. Blood Soak (AM_Dirtdefinition_BloodEffect: Blood_Red 86,20,24, Roughness 0.20..0.30)
        if (charBloodSoak > 0.005) {
          float bNoise = cFbm(vBindPos * 9.5 + vec3(4.1, 2.2, 1.7));
          float dripNoise = cNoise3(vec3(vBindPos.x * 26.0, vBindPos.y * 5.0, vBindPos.z * 26.0));
          float bMask = smoothstep(0.62 - charBloodSoak * 0.52, 0.78 - charBloodSoak * 0.40, bNoise * 0.68 + dripNoise * 0.38);
          float bAmt = clamp(bMask * (0.35 + 0.85 * charBloodSoak), 0.0, 0.95);
          vec3 bloodCol = mix(vec3(0.337, 0.078, 0.094), vec3(0.14, 0.018, 0.022), bNoise);
          base = mix(base, bloodCol, bAmt);
          rough = mix(rough, mix(0.20, 0.30, bNoise), bAmt);
          clearcoat = max(clearcoat, bAmt * 0.78);
          ccRough = mix(ccRough, 0.16, bAmt);
        }

        // 5. 3D Projected Wounds (Bind-Space Gunshots, Blood Spatters, Bruises, Scrapes & Burns)
        if (charWoundCount > 0) {
          for (int wi = 0; wi < 8; wi++) {
            if (wi >= charWoundCount) break;
            vec4 wp = charWoundPos[wi];
            vec4 wpr = charWoundParam[wi];
            vec3 d = vBindPos - wp.xyz;
            float rad = max(wp.w, 0.015);
            float dist = length(d);
            float dripLen = wpr.z;
            // Fast sphere/drip reject
            if (dist > rad * 2.6 && !(d.y < 0.0 && d.y > -dripLen && length(d.xz) < rad * 0.65)) continue;
            // Tangent-plane UVs in bind space
            vec3 upRef = abs(gN.y) < 0.92 ? vec3(0.0, 1.0, 0.0) : vec3(1.0, 0.0, 0.0);
            vec3 tX = normalize(cross(upRef, gN));
            vec3 tY = cross(gN, tX);
            vec2 proj = vec2(dot(d, tX), dot(d, tY)) / (rad * 2.0) + 0.5;
            float radial = smoothstep(rad * 1.45, rad * 0.18, dist);
            float wMask = 0.0;
            vec3 wCol = vec3(0.337, 0.078, 0.094);
            float wRough = 0.22;
            float wCC = 0.82;
            if (wpr.x < 0.5) { // 0: Gunshot / Blood Spatter (uses real ttdb_vfx bloodDecalTex when loaded)
              float spNoise = cFbm(vBindPos * 38.0 + wpr.w * 7.1);
              if (useBloodDecal && proj.x > 0.0 && proj.x < 1.0 && proj.y > 0.0 && proj.y < 1.0) {
                vec4 dt = texture(bloodDecalTex, clamp(proj, 0.0, 1.0));
                float alphaD = bloodDecalTintable ? max(dt.a, dot(dt.rgb, vec3(0.333))) : max(dt.a, (dt.r - dt.g) * 1.6);
                wMask = clamp(alphaD * smoothstep(rad * 1.35, rad * 0.7, dist) * 1.35, 0.0, 1.0);
                wCol = bloodDecalTintable ? vec3(0.337, 0.078, 0.094) : mix(vec3(0.337, 0.078, 0.094), dt.rgb * 0.65, 0.55);
              } else {
                wMask = smoothstep(0.30, 0.68, radial + (spNoise - 0.45) * 0.55);
              }
              // Dark bullet entry cavity at center
              float hole = smoothstep(rad * 0.26, rad * 0.06, dist);
              wCol = mix(wCol, vec3(0.035, 0.006, 0.008), hole * 0.92);
              wMask = max(wMask, hole);
              // Gravity blood drip streak below wound
              if (d.y < 0.0 && d.y > -dripLen) {
                float dLat = length(d.xz) / max(rad * 0.28, 0.006);
                float dFade = 1.0 - (-d.y / max(dripLen, 0.01));
                float dWig = cNoise3(vec3(vBindPos.x * 45.0, vBindPos.y * 12.0, wpr.w * 11.0));
                float drip = smoothstep(1.0, 0.15, dLat + (dWig - 0.5) * 0.65) * dFade * 0.88;
                wMask = max(wMask, drip);
              }
            } else if (wpr.x < 1.5) { // 1: Bruise / Contusion
              float bN = cFbm(vBindPos * 26.0 + wpr.w * 5.3);
              float core = smoothstep(rad * 1.15, rad * 0.2, dist);
              float ring = smoothstep(rad * 1.55, rad * 0.75, dist) * (1.0 - core * 0.6);
              wCol = mix(vec3(0.42, 0.36, 0.16), vec3(0.20, 0.10, 0.24), core);
              wMask = clamp((core * 0.85 + ring * 0.45) * (0.65 + 0.55 * bN), 0.0, 0.82);
              wRough = 0.58; wCC = 0.05;
            } else if (wpr.x < 2.5) { // 2: Scrape / Road Rash
              float sN = cNoise3(vec3(vBindPos.x * 60.0, vBindPos.y * 16.0, vBindPos.z * 60.0));
              wMask = clamp(radial * smoothstep(0.28, 0.72, sN) * 1.1, 0.0, 0.88);
              wCol = mix(vec3(0.56, 0.18, 0.16), vec3(0.32, 0.06, 0.07), sN);
              wRough = 0.38; wCC = 0.28;
            } else { // 3: Burn / Scorch
              float cN = cFbm(vBindPos * 24.0 + wpr.w * 9.7);
              float core = smoothstep(rad * 0.95, rad * 0.15, dist);
              float halo = smoothstep(rad * 1.60, rad * 0.75, dist);
              wCol = mix(vec3(0.52, 0.16, 0.12), vec3(0.04, 0.035, 0.035), clamp(core * 1.35 + cN * 0.35, 0.0, 1.0));
              wMask = clamp(halo * (0.55 + 0.55 * cN), 0.0, 0.94);
              wRough = mix(0.42, 0.88, core); wCC = 0.04;
            }
            wMask *= clamp(wpr.y, 0.0, 1.0);
            if (wMask > 0.01) {
              base = mix(base, wCol, wMask);
              rough = mix(rough, wRough, wMask);
              clearcoat = max(clearcoat, wCC * wMask);
              ccRough = mix(ccRough, wRough * 0.75, wMask);
            }
          }
        }

        // 6. WET_SKIN / WET_UNDERWATER_SKIN (height-line wet film, skin 0.49412 porosity clamp, droplets & eye wetness)
        float hMask = smoothstep(charWaterHeight + 0.06, charWaterHeight - 0.06, vBindPos.y);
        float uwMask = charUnderwater * smoothstep(charWaterLine + 0.05, charWaterLine - 0.05, vBindPos.y);
        fxUwEff = uwMask;
        fxWetEff = clamp(max(charWetness * hMask, uwMask), 0.0, 1.0);
        if (fxWetEff > 0.005) {
          float soakEff = clamp(max(charSoak, fxWetEff * 0.75) * hMask, 0.0, 1.0);
          if (isSkin) {
            // RAGE 9 WET_SKIN: subtle rich melanin/subsurface darkening (clamped by skin factor 0.49412), never muddy
            base = mix(base, pow(max(base, vec3(0.001)), vec3(1.18)) * vec3(0.95, 0.91, 0.89), fxWetEff * 0.49412);
            rough = mix(rough, 0.11, fxWetEff * 0.85);
            clearcoat = max(clearcoat, fxWetEff * 0.92 * (1.0 - uwMask * 0.65));
            ccRough = mix(ccRough, 0.045, fxWetEff);
            // Animated micro-droplets & gravity rain streaks on skin
            if (charDroplets > 0.01 && uwMask < 0.8) {
              float strk = cNoise3(vec3(vBindPos.x * 48.0, vBindPos.y * 14.0 + charTime * 2.2, vBindPos.z * 48.0));
              float bead = smoothstep(0.66, 0.86, cNoise3(vBindPos * 68.0 + vec3(0.0, charTime * 0.35, 0.0)));
              float dStr = (bead * 0.75 + smoothstep(0.68, 0.88, strk) * 0.55) * charDroplets * fxWetEff * (1.0 - uwMask);
              vec3 dPert = vec3(
                cNoise3(vBindPos * 68.0 + vec3(0.15, charTime * 0.35, 0.0)) - 0.5,
                cNoise3(vBindPos * 68.0 + vec3(0.0, charTime * 0.35 + 0.15, 0.0)) - 0.5,
                0.0
              );
              n = normalize(n + dPert * dStr * 0.48);
              rough = mix(rough, 0.04, dStr * 0.65);
            }
          } else if (!isEye) {
            // Porous fabric/garment soak darkening (stronger than skin)
            base *= mix(1.0, 0.52, soakEff * 0.82);
            rough = mix(rough, 0.24, fxWetEff * 0.72);
            clearcoat = max(clearcoat, fxWetEff * 0.48 * (1.0 - uwMask * 0.65));
            ccRough = mix(ccRough, 0.12, fxWetEff);
          }
        }
        if (isEye && charEyeWetness > 0.005) {
          rough = mix(rough, 0.03, charEyeWetness);
          clearcoat = max(clearcoat, 0.95);
          ccRough = mix(0.08, 0.02, charEyeWetness);
        }

        // 7. WET_UNDERWATER_SKIN: Beer-Lambert depth absorption + animated caustics
        if (fxUwEff > 0.005) {
          float depth = max(0.0, charWaterLine - vBindPos.y);
          vec3 absorb = exp(-vec3(1.25, 0.36, 0.15) * (0.45 + depth * 1.15));
          base = mix(base, base * absorb * vec3(0.88, 1.06, 1.14), fxUwEff * 0.82);
          if (charCaustics > 0.005) {
            vec2 cp = vW.xz * 7.5 + vW.y * 1.2;
            float w1 = sin(cp.x * 2.1 + charTime * 2.1 + cos(cp.y * 1.9 - charTime * 1.5));
            float w2 = sin(cp.y * 2.6 - charTime * 1.8 + cos(cp.x * 2.3 + charTime * 1.3));
            float caust = pow(clamp(1.0 - abs(w1 + w2) * 0.48, 0.0, 1.0), 5.0);
            fxCausticBoost = caust * max(n.y * 0.75 + 0.35, 0.0) * charCaustics * fxUwEff;
          }
        }
      }

      // Physically-based Cook-Torrance GGX + Studio/Sky IBL + Clearcoat + ACES Tone Mapping
      vec3 baseLin = toLinear(base);
      float NdotV = max(dot(n, v), 0.001);
      vec3 R = reflect(-v, n);
      vec3 F0 = mix(vec3(0.04), baseLin, metal);

      float sh = evalShadow(vW, gN);
      vec3 l3 = uRimDir;
      float pedKeyScale = charFxOn ? clamp(charPedLightMult, 0.4, 4.5) : 1.0;
      vec3 kCol = uKeyCol * pedKeyScale;
      vec3 direct = evalLight(n, v, l1, kCol, baseLin, rough, metal, clearcoat, ccRough) * sh
                  + evalLight(n, v, l2, uFillCol, baseLin, rough, metal, clearcoat, ccRough)
                  + evalLight(n, v, l3, uRimCol, baseLin, rough, metal, clearcoat, ccRough);
      // Wrapped-diffuse warm subsurface scattering across shadow terminators for human skin.
      if(matMode == 3){
        float w1 = clamp((dot(n, l1) + 0.48) / 1.48, 0.0, 1.0);
        float w2 = clamp((dot(n, l2) + 0.45) / 1.45, 0.0, 1.0);
        float w3 = clamp((dot(n, l3) + 0.35) / 1.35, 0.0, 1.0);
        float sss1 = max(w1 - max(dot(n, l1), 0.0), 0.0) * mix(0.45, 1.0, sh);
        float sss2 = max(w2 - max(dot(n, l2), 0.0), 0.0);
        float sss3 = max(w3 - max(dot(n, l3), 0.0), 0.0) * 0.55;
        direct += baseLin * sssTint * (sss1 * kCol + sss2 * uFillCol + sss3 * uRimCol) * (0.30 / PI);
      }
      // Hemispherical diffuse IBL + roughness-filtered specular IBL
      vec3 diffEnv = mix(uAmbBot, uAmbTop, 0.5 + 0.5 * n.y);
      vec3 specEnv = sampleEnv(R, rough);
      vec3 F_ibl = F0 + (max(vec3(1.0 - rough), F0) - F0) * pow(1.0 - NdotV, 5.0);
      vec3 kd_ibl = (1.0 - F_ibl) * (1.0 - metal);
      vec3 ambient = (kd_ibl * baseLin * diffEnv + F_ibl * specEnv * (0.85 - 0.35 * rough)) * ao;

      // Clearcoat IBL + optional automotive pearlescent Fresnel shift
      if(clearcoat > 0.01){
        float cF = 0.04 + 0.96 * pow(1.0 - NdotV, 5.0);
        ambient += sampleEnv(R, ccRough) * (cF * clearcoat * 0.95) * ao;
        if(length(pearlTint) > 0.001){
          ambient += toLinear(pearlTint) * pow(1.0 - NdotV, 2.2) * (0.35 + 0.65 * max(dot(n, l1), 0.0)) * ao;
        }
      }

      vec3 litLin = direct + ambient;
      if (charFxOn) {
        // Underwater caustics additive light
        if (fxCausticBoost > 0.002) {
          litLin += baseLin * vec3(0.28, 0.92, 1.05) * fxCausticBoost * 1.45;
        }
        // Molly / Rampage ped_light_fresnel extreme rim glow
        if (charPedFresnel > 0.01) {
          float rim = pow(1.0 - NdotV, 2.4) * charPedFresnel;
          litLin += vec3(1.15, 0.82, 0.98) * rim * 0.95;
        }
        // Cocaine UV Party Mode indigo blacklight fluorescence
        if (charUvParty > 0.01) {
          float uvRim = pow(1.0 - NdotV, 1.8);
          float lumaB = dot(base, vec3(0.299, 0.587, 0.114));
          litLin = mix(litLin, litLin * vec3(0.45, 0.35, 1.45) + vec3(0.18, 0.06, 0.52) * (uvRim + smoothstep(0.55, 0.95, lumaB)), charUvParty * 0.68);
        }
        // Mandy_Spangled brdf_glints microfacet sparkle glints
        if (charGlints > 0.01) {
          vec3 gCell = floor(vBindPos * 110.0 + R * 4.5);
          float gRand = cHash31(gCell + floor(charTime * 10.0) * 0.07);
          float sparkle = smoothstep(0.986 - charGlints * 0.016, 0.999, gRand) * pow(max(dot(n, h), 0.0), 3.0);
          vec3 glintCol = mix(vec3(1.0, 0.92, 0.72), vec3(0.72, 0.88, 1.0), fract(gRand * 37.3));
          litLin += glintCol * sparkle * charGlints * 4.5;
        }
      }
      vec3 lit = toSRGB(acesTone(litLin * uExposure));
      if(passType == 3 && lumaAlpha) lit = mix(lit, base * 1.25, 0.75);
      if(highlight) lit = mix(lit, vec3(0.45, 0.92, 0.72), 0.32);
      float outA = passType >= 2 ? (passType == 4 && effA > 0.96 ? 0.72 : clamp(effA, 0.0, 1.0)) : 1.0;
      o = vec4(lit, outA);
    }`;

  function compile(gl, type, src) {
    const s = gl.createShader(type);
    gl.shaderSource(s, src);
    gl.compileShader(s);
    if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw Error(gl.getShaderInfoLog(s));
    return s;
  }
  const mat = {
    mul(a, b) {
      const o = new Float32Array(16);
      for (let i = 0; i < 4; i++) for (let j = 0; j < 4; j++) {
        let s = 0;
        for (let k = 0; k < 4; k++) s += a[k * 4 + j] * b[i * 4 + k];
        o[i * 4 + j] = s;
      }
      return o;
    },
    persp(fov, asp, n, f) {
      const t = 1 / Math.tan(fov / 2), o = new Float32Array(16);
      o[0] = t / asp; o[5] = t; o[10] = (f + n) / (n - f); o[11] = -1; o[14] = (2 * f * n) / (n - f);
      return o;
    },
    ortho(l, r, b, t, n, f) {
      const o = new Float32Array(16);
      o[0] = 2 / (r - l); o[5] = 2 / (t - b); o[10] = -2 / (f - n); o[15] = 1;
      o[12] = -(r + l) / (r - l); o[13] = -(t + b) / (t - b); o[14] = -(f + n) / (f - n);
      return o;
    },
    look(e, c, u) {
      const z = norm(sub(e, c)), x = norm(cross(u, z)), y = cross(z, x);
      return new Float32Array([x[0], y[0], z[0], 0, x[1], y[1], z[1], 0, x[2], y[2], z[2], 0,
        -dot(x, e), -dot(y, e), -dot(z, e), 1]);
    },
  };
  const sub = (a, b) => [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
  const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
  const cross = (a, b) => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
  const norm = (a) => { const l = Math.hypot(...a) || 1; return [a[0] / l, a[1] / l, a[2] / l]; };

  function parseGlb(buf) {
    const dv = new DataView(buf);
    if (dv.getUint32(0, true) !== 0x46546c67) throw Error("Not a GLB file");
    const jlen = dv.getUint32(12, true);
    const doc = JSON.parse(new TextDecoder().decode(new Uint8Array(buf, 20, jlen)));
    const binStart = 20 + jlen + 8;
    const binLen = dv.getUint32(20 + jlen, true);
    return { doc, bin: new Uint8Array(buf, binStart, binLen) };
  }
  function accessor(doc, bin, i) {
    const a = doc.accessors[i], v = doc.bufferViews[a.bufferView];
    const width = { SCALAR: 1, VEC2: 2, VEC3: 3, VEC4: 4 }[a.type];
    const Type = { 5126: Float32Array, 5123: Uint16Array, 5125: Uint32Array, 5121: Uint8Array }[a.componentType];
    const off = bin.byteOffset + (v.byteOffset || 0) + (a.byteOffset || 0);
    const stride = v.byteStride || 0;
    if (stride && stride !== width * Type.BYTES_PER_ELEMENT) {
      const out = new Type(a.count * width), src = new DataView(bin.buffer);
      for (let k = 0; k < a.count; k++) for (let c = 0; c < width; c++)
        out[k * width + c] = Type === Float32Array ? src.getFloat32(off + k * stride + c * 4, true) : src.getUint16(off + k * stride + c * 2, true);
      return out;
    }
    return new Type(bin.buffer.slice(off, off + a.count * width * Type.BYTES_PER_ELEMENT));
  }
  function nodeMatrix(n) {
    if (n.matrix) return new Float32Array(n.matrix);
    const m = new Float32Array([1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]);
    if (n.translation) { m[12] = n.translation[0]; m[13] = n.translation[1]; m[14] = n.translation[2]; }
    return m;
  }

  const EXTRA_TEX_FIELDS = [
    "texture", "normalTexture", "layer1Tex", "layer2Tex", "layer3Tex",
    "maskTex", "tintTex", "tintPalTex", "specTex", "spec2Tex", "dustTex",
  ];
  const _imgAnalysisCache = new WeakMap();
  let _probeCanvas = null, _probeCtx = null;

  function analyzeImage(bmp) {
    if (!bmp) return { isMagenta: false, isCutout: false, isBilinearMask: false };
    const cached = _imgAnalysisCache.get(bmp);
    if (cached) return cached;
    try {
      const w = Math.max(1, Math.min(64, bmp.width || 64));
      const h = Math.max(1, Math.min(64, bmp.height || 64));
      if (!_probeCanvas) {
        _probeCanvas = document.createElement("canvas");
        _probeCtx = _probeCanvas.getContext("2d", { willReadFrequently: true });
      }
      if (_probeCanvas.width !== w || _probeCanvas.height !== h) {
        _probeCanvas.width = w;
        _probeCanvas.height = h;
      }
      _probeCtx.clearRect(0, 0, w, h);
      _probeCtx.drawImage(bmp, 0, 0, w, h);
      const d = _probeCtx.getImageData(0, 0, w, h).data;
      const n = w * h;
      let magHits = 0, clear = 0, solid = 0;
      let rSum = 0, rSq = 0, gSum = 0, gSq = 0, bSum = 0, bSq = 0;
      for (let i = 0; i < d.length; i += 4) {
        const r = d[i], g = d[i + 1], b = d[i + 2], a = d[i + 3];
        if (r > 200 && g < 60 && b > 200) magHits++;
        if (a < 16) clear++;
        else if (a > 239) solid++;
        const rf = r / 255, gf = g / 255, bf = b / 255;
        rSum += rf; rSq += rf * rf;
        gSum += gf; gSq += gf * gf;
        bSum += bf; bSq += bf * bf;
      }
      const isMagenta = magHits * 4 >= n;
      const clearFrac = clear / n;
      const binaryFrac = (clear + solid) / n;
      const isCutout = clearFrac > 0.02 && binaryFrac > 0.85;
      const rStd = Math.sqrt(Math.max(0, rSq / n - (rSum / n) ** 2));
      const gStd = Math.sqrt(Math.max(0, gSq / n - (gSum / n) ** 2));
      const bStd = Math.sqrt(Math.max(0, bSq / n - (bSum / n) ** 2));
      const isBilinearMask = rStd < 0.015 && gStd > 0.03 && bStd > 0.03;
      const res = { isMagenta, isCutout, isBilinearMask };
      _imgAnalysisCache.set(bmp, res);
      return res;
    } catch {
      return { isMagenta: false, isCutout: false, isBilinearMask: false };
    }
  }

  const STUDIO_LIGHT_PRESETS = [
    {
      id: "studio_neutral",
      label: "Neutral Studio",
      short: "Neutral",
      swatch: "linear-gradient(135deg,#fff8ec 0%,#9ab8d8 52%,#1c272c 100%)",
      keyDir: [0.38, 0.82, 0.45],
      keyCol: [2.35, 2.28, 2.15],
      fillDir: [-0.55, 0.25, -0.60],
      fillCol: [0.68, 0.78, 0.92],
      rimDir: [-0.22, 0.42, -0.88],
      rimCol: [0.50, 0.56, 0.65],
      ambBot: [0.11, 0.13, 0.15],
      ambTop: [0.38, 0.44, 0.52],
      envGround: [0.055, 0.068, 0.078],
      envHorizon: [0.46, 0.52, 0.58],
      envZenith: [0.20, 0.34, 0.54],
      envStrip: 1.0,
      matchBg: { mode: 0, top: [0.09, 0.13, 0.16], mid: [0.11, 0.15, 0.18], bot: [0.05, 0.08, 0.095], vig: 0.38, grid: [0.18, 0.24, 0.26] },
    },
    {
      id: "studio_soft",
      label: "Beauty / Softbox",
      short: "Softbox",
      swatch: "linear-gradient(135deg,#ffffff 0%,#f5e4d8 52%,#42474e 100%)",
      keyDir: [0.16, 0.48, 0.86],
      keyCol: [2.05, 1.98, 1.94],
      fillDir: [-0.72, 0.36, 0.52],
      fillCol: [1.18, 1.15, 1.18],
      rimDir: [0.45, 0.58, -0.68],
      rimCol: [0.75, 0.80, 0.88],
      ambBot: [0.22, 0.22, 0.24],
      ambTop: [0.54, 0.55, 0.58],
      envGround: [0.14, 0.14, 0.15],
      envHorizon: [0.62, 0.62, 0.64],
      envZenith: [0.48, 0.50, 0.54],
      envStrip: 1.45,
      matchBg: { mode: 0, top: [0.26, 0.27, 0.29], mid: [0.34, 0.35, 0.37], bot: [0.19, 0.20, 0.22], vig: 0.28, grid: [0.42, 0.44, 0.47] },
    },
    {
      id: "studio_dramatic",
      label: "Dramatic / Rembrandt",
      short: "Dramatic",
      swatch: "linear-gradient(135deg,#ffcf96 0%,#263242 56%,#090c10 100%)",
      keyDir: [0.76, 0.56, 0.34],
      keyCol: [2.95, 2.52, 2.04],
      fillDir: [-0.72, 0.18, 0.20],
      fillCol: [0.26, 0.38, 0.58],
      rimDir: [-0.52, 0.45, -0.72],
      rimCol: [1.35, 1.52, 1.85],
      ambBot: [0.04, 0.05, 0.07],
      ambTop: [0.16, 0.20, 0.28],
      envGround: [0.025, 0.03, 0.04],
      envHorizon: [0.22, 0.26, 0.34],
      envZenith: [0.08, 0.12, 0.22],
      envStrip: 0.45,
      matchBg: { mode: 0, top: [0.045, 0.055, 0.075], mid: [0.075, 0.085, 0.105], bot: [0.02, 0.025, 0.035], vig: 0.55, grid: [0.16, 0.18, 0.22] },
    },
    {
      id: "vice_golden",
      label: "Vice Golden Hour",
      short: "Golden Hour",
      swatch: "linear-gradient(135deg,#ffb156 0%,#e06b74 52%,#2a2346 100%)",
      keyDir: [0.84, 0.28, 0.46],
      keyCol: [3.10, 2.02, 1.18],
      fillDir: [-0.62, 0.44, 0.42],
      fillCol: [0.64, 0.54, 0.94],
      rimDir: [-0.42, 0.34, -0.84],
      rimCol: [1.55, 0.92, 0.52],
      ambBot: [0.14, 0.10, 0.11],
      ambTop: [0.36, 0.28, 0.46],
      envGround: [0.10, 0.065, 0.06],
      envHorizon: [0.78, 0.44, 0.30],
      envZenith: [0.20, 0.18, 0.42],
      envStrip: 0.15,
      matchBg: { mode: 0, top: [0.15, 0.12, 0.27], mid: [0.38, 0.21, 0.25], bot: [0.18, 0.11, 0.10], vig: 0.34, grid: [0.36, 0.24, 0.24] },
    },
    {
      id: "vice_noon",
      label: "Miami Daylight",
      short: "Daylight",
      swatch: "linear-gradient(135deg,#fffef2 0%,#68b6f2 54%,#b8c9d4 100%)",
      keyDir: [0.28, 0.90, 0.34],
      keyCol: [3.00, 2.92, 2.66],
      fillDir: [-0.54, 0.52, 0.46],
      fillCol: [0.86, 1.06, 1.32],
      rimDir: [0.18, 0.44, -0.88],
      rimCol: [0.70, 0.80, 0.90],
      ambBot: [0.24, 0.22, 0.19],
      ambTop: [0.46, 0.62, 0.84],
      envGround: [0.24, 0.21, 0.18],
      envHorizon: [0.74, 0.84, 0.94],
      envZenith: [0.22, 0.52, 0.92],
      envStrip: 0.0,
      matchBg: { mode: 0, top: [0.30, 0.54, 0.80], mid: [0.64, 0.78, 0.89], bot: [0.40, 0.44, 0.47], vig: 0.18, grid: [0.32, 0.38, 0.42] },
    },
    {
      id: "vice_neon",
      label: "Vice Night / Neon",
      short: "Vice Neon",
      swatch: "linear-gradient(135deg,#ff2e97 0%,#6b2fd9 48%,#1ce6ff 100%)",
      keyDir: [0.68, 0.38, 0.62],
      keyCol: [2.65, 0.72, 1.82],
      fillDir: [-0.76, 0.26, 0.50],
      fillCol: [0.36, 1.72, 2.25],
      rimDir: [0.12, 0.48, -0.87],
      rimCol: [1.42, 0.52, 2.15],
      ambBot: [0.06, 0.04, 0.12],
      ambTop: [0.18, 0.11, 0.30],
      envGround: [0.04, 0.025, 0.08],
      envHorizon: [0.42, 0.14, 0.48],
      envZenith: [0.06, 0.06, 0.22],
      envStrip: 0.35,
      matchBg: { mode: 0, top: [0.055, 0.045, 0.16], mid: [0.16, 0.065, 0.22], bot: [0.035, 0.08, 0.14], vig: 0.46, grid: [0.24, 0.18, 0.36] },
    },
    {
      id: "warm_indoor",
      label: "Warm Interior",
      short: "Interior",
      swatch: "linear-gradient(135deg,#ffd9a0 0%,#b8895c 52%,#2a231c 100%)",
      keyDir: [0.24, 0.78, 0.58],
      keyCol: [2.55, 2.02, 1.40],
      fillDir: [-0.74, 0.30, 0.34],
      fillCol: [0.76, 0.86, 1.02],
      rimDir: [0.35, 0.42, -0.84],
      rimCol: [0.88, 0.68, 0.45],
      ambBot: [0.16, 0.12, 0.085],
      ambTop: [0.40, 0.32, 0.22],
      envGround: [0.12, 0.09, 0.065],
      envHorizon: [0.54, 0.42, 0.30],
      envZenith: [0.46, 0.36, 0.25],
      envStrip: 0.65,
      matchBg: { mode: 0, top: [0.20, 0.16, 0.12], mid: [0.24, 0.20, 0.15], bot: [0.11, 0.09, 0.07], vig: 0.40, grid: [0.30, 0.25, 0.20] },
    },
    {
      id: "overcast",
      label: "Overcast / Albedo Check",
      short: "Overcast",
      swatch: "linear-gradient(135deg,#e2e7ec 0%,#9ea7b0 52%,#565e66 100%)",
      keyDir: [0.18, 0.92, 0.35],
      keyCol: [1.35, 1.36, 1.38],
      fillDir: [-0.52, 0.56, 0.56],
      fillCol: [1.05, 1.08, 1.12],
      rimDir: [0.25, 0.52, -0.82],
      rimCol: [0.82, 0.85, 0.88],
      ambBot: [0.34, 0.35, 0.36],
      ambTop: [0.64, 0.66, 0.70],
      envGround: [0.24, 0.25, 0.26],
      envHorizon: [0.66, 0.68, 0.71],
      envZenith: [0.54, 0.58, 0.63],
      envStrip: 0.20,
      matchBg: { mode: 0, top: [0.40, 0.43, 0.46], mid: [0.48, 0.50, 0.52], bot: [0.30, 0.32, 0.34], vig: 0.18, grid: [0.34, 0.36, 0.38] },
    },
    {
      id: "rim_silhouette",
      label: "Backlit Rim / Contour",
      short: "Backlit Rim",
      swatch: "linear-gradient(135deg,#88d4ff 0%,#1b2330 52%,#06080c 100%)",
      keyDir: [-0.36, 0.44, -0.82],
      keyCol: [2.85, 2.95, 3.15],
      fillDir: [0.58, 0.32, -0.74],
      fillCol: [1.85, 1.62, 1.32],
      rimDir: [0.05, 0.25, 0.96],
      rimCol: [0.46, 0.50, 0.58],
      ambBot: [0.05, 0.06, 0.08],
      ambTop: [0.14, 0.17, 0.22],
      envGround: [0.02, 0.025, 0.035],
      envHorizon: [0.18, 0.22, 0.28],
      envZenith: [0.07, 0.11, 0.19],
      envStrip: 0.30,
      matchBg: { mode: 0, top: [0.035, 0.048, 0.072], mid: [0.065, 0.082, 0.112], bot: [0.02, 0.025, 0.035], vig: 0.52, grid: [0.15, 0.18, 0.24] },
    },
  ];
  const STUDIO_BG_PRESETS = [
    { id: "match", label: "Match Lighting", swatch: "linear-gradient(135deg,#ffd296 0%,#527aa3 50%,#192229 100%)", mode: 0 },
    { id: "studio_dark", label: "Dark Cyclorama", swatch: "linear-gradient(180deg,#152026 0%,#1c2930 52%,#0c1317 100%)", mode: 0, top: [0.082, 0.122, 0.142], mid: [0.112, 0.160, 0.184], bot: [0.048, 0.074, 0.088], vig: 0.38, grid: [0.18, 0.24, 0.26] },
    { id: "studio_mid", label: "Neutral Gray (18%)", swatch: "linear-gradient(180deg,#44484d 0%,#565a60 52%,#34383d 100%)", mode: 0, top: [0.27, 0.28, 0.30], mid: [0.35, 0.36, 0.38], bot: [0.20, 0.21, 0.23], vig: 0.26, grid: [0.44, 0.46, 0.49] },
    { id: "studio_light", label: "White Seamless", swatch: "linear-gradient(180deg,#f2f4f7 0%,#e4e7ec 55%,#c5cbd4 100%)", mode: 0, top: [0.92, 0.93, 0.95], mid: [0.87, 0.89, 0.91], bot: [0.74, 0.77, 0.80], vig: 0.14, grid: [0.62, 0.65, 0.69] },
    { id: "env_sky", label: "3D Environment Dome", swatch: "linear-gradient(180deg,#2d5da6 0%,#98bed8 55%,#3b464d 100%)", mode: 1, grid: [0.30, 0.36, 0.40] },
    { id: "vice_sunset", label: "Vice City Sunset", swatch: "linear-gradient(180deg,#231d47 0%,#8c3e5e 52%,#d67d54 100%)", mode: 0, top: [0.13, 0.11, 0.28], mid: [0.54, 0.24, 0.36], bot: [0.76, 0.42, 0.28], vig: 0.30, grid: [0.42, 0.26, 0.26] },
    { id: "vice_neon", label: "Neon Night", swatch: "linear-gradient(180deg,#0b0a24 0%,#2b1240 52%,#0c2334 100%)", mode: 0, top: [0.045, 0.04, 0.14], mid: [0.17, 0.07, 0.25], bot: [0.05, 0.14, 0.21], vig: 0.46, grid: [0.24, 0.18, 0.36] },
    { id: "chroma_green", label: "Chroma Key Green", swatch: "#00b140", mode: 2, mid: [0.0, 0.694, 0.251], grid: [0.0, 0.50, 0.18] },
    { id: "solid", label: "Custom Solid Color", swatch: "conic-gradient(#e05a5a,#e8c55a,#58c472,#5a9ee0,#b46be0,#e05a5a)", mode: 2, grid: [0.35, 0.38, 0.42] },
  ];
  function hexToRgb01(hex, fb = [0.11, 0.13, 0.16]) {
    if (typeof hex !== "string") return fb;
    const m = /^#?([0-9a-f]{6})$/i.exec(hex.trim());
    if (!m) return fb;
    const n = parseInt(m[1], 16);
    return [((n >> 16) & 255) / 255, ((n >> 8) & 255) / 255, (n & 255) / 255];
  }
  function rotYVec3(v, rad) {
    const c = Math.cos(rad), s = Math.sin(rad);
    return norm([v[0] * c + v[2] * s, v[1], -v[0] * s + v[2] * c]);
  }

  class ModelView {
    static analyzeImage(bmp) { return analyzeImage(bmp); }
    static get LIGHT_PRESETS() { return STUDIO_LIGHT_PRESETS; }
    static get BG_PRESETS() { return STUDIO_BG_PRESETS; }
    constructor(canvas) {
      this.canvas = canvas;
      const gl = canvas.getContext("webgl2", { antialias: true, premultipliedAlpha: false, powerPreference: "high-performance" });
      if (!gl) throw Error("WebGL2 is not available in this browser");
      this.gl = gl;
      this.extAniso = gl.getExtension("EXT_texture_filter_anisotropic");
      this.maxAniso = this.extAniso ? Math.min(16, gl.getParameter(this.extAniso.MAX_TEXTURE_MAX_ANISOTROPY_EXT) || 1) : 1;
      const p = gl.createProgram();
      gl.attachShader(p, compile(gl, gl.VERTEX_SHADER, VS));
      gl.attachShader(p, compile(gl, gl.FRAGMENT_SHADER, FS));
      gl.linkProgram(p);
      if (!gl.getProgramParameter(p, gl.LINK_STATUS)) throw Error(gl.getProgramInfoLog(p));
      this.prog = p;
      this.loc = {};
      for (const k of [
        "mvp", "model", "flipV", "useSkin", "hasSkin8", "boneTex", "mode", "texMode", "matMode", "passType",
        "color", "pal0", "pal1", "pal2", "eye", "skinGain",
        "tex", "normTex", "layer1Tex", "layer2Tex", "layer3Tex", "maskTex", "tintTex", "tintPalTex",
        "specTex", "spec2Tex", "dustTex", "liveryTex", "lutArrayTex", "shadowTex", "lightVP", "useShadow", "shadowBias",
        "useTex", "useNorm", "useColor", "hasNormals", "alphaCut", "highlight", "useTintPal",
        "useSpec", "useSpec2", "useDust", "useLivery", "useLutArray",
        "tintRow", "tintRowLen", "tintPalWidth", "lutSlices",
        "hasUV1", "hasBlendW", "hasVertAlpha", "hasL1", "hasL2", "hasL3", "useMask", "useBilinearMask", "useTintTex", "lumaAlpha",
        "vehPrimary", "vehSecondary", "vehPearlCol",
        "vehPaintRow1", "vehPaintRow2", "vehPaintSlice", "vehPearl", "vehClearcoat", "vehDirt", "vehWear", "vehTwoTone", "vehInterior", "vehLutExact",
        "wpnFinish", "wpnWear", "liveryScale",
        "charFxOn", "useBloodDecal", "bloodDecalTintable", "bloodDecalTex",
        "charTime", "charWetness", "charSoak", "charWaterHeight", "charDroplets", "charEyeWetness",
        "charDust", "charDirtAmt", "charDirtSkinStr", "charDirtClothStr", "charDirtCol", "charDirtRough",
        "charUnderwater", "charWaterLine", "charCaustics",
        "charSunTan", "charSunBurn", "charFlush", "charTanLines",
        "charGlints", "charPedLightMult", "charPedFresnel", "charUvParty",
        "charBloodSoak", "charWoundCount", "charWoundPos", "charWoundParam",
        "uKeyDir", "uKeyCol", "uFillDir", "uFillCol", "uRimDir", "uRimCol",
        "uEnvGround", "uEnvHorizon", "uEnvZenith", "uAmbBot", "uAmbTop",
        "uExposure", "uEnvStrip", "uShadowMin",
      ]) this.loc[k] = gl.getUniformLocation(p, k);
      // Full-screen GPU Post-FX shader for RAGE 9 Drug / Status Timecycle Modifiers (Mandy_*, Cocaine, Drunk, Trips)
      this.postProg = null;
      this.postLoc = {};
      this._postFbo = null; this._postTex = null; this._postDepth = null;
      this._trailFbo = null; this._trailTex = null; this._postW = 0; this._postH = 0;
      this._postVao = gl.createVertexArray();
      try {
        const pp = gl.createProgram();
        gl.attachShader(pp, compile(gl, gl.VERTEX_SHADER, POST_VS));
        gl.attachShader(pp, compile(gl, gl.FRAGMENT_SHADER, POST_FS));
        gl.linkProgram(pp);
        if (gl.getProgramParameter(pp, gl.LINK_STATUS)) {
          this.postProg = pp;
          for (const k of [
            "sceneTex", "trailTex", "pfTime", "pfIntensity", "pfBloom", "pfLensK1", "pfLensK2",
            "pfChromK1", "pfChromK2", "pfChromCol", "pfVigCol", "pfVigInt", "pfVigIn", "pfVigOut",
            "pfVigBloom", "pfDesat", "pfShCon", "pfHlCon", "pfDtCon", "pfNoise", "pfExposure",
            "pfTrail", "pfRedShadow", "pfUvParty",
          ]) this.postLoc[k] = gl.getUniformLocation(pp, k);
        }
      } catch (_) { this.postProg = null; }
      // Studio Backdrop / 3D Environment Dome shader
      this.bgProg = null;
      this.bgLoc = {};
      try {
        const bp = gl.createProgram();
        gl.attachShader(bp, compile(gl, gl.VERTEX_SHADER, POST_VS));
        gl.attachShader(bp, compile(gl, gl.FRAGMENT_SHADER, BG_FS));
        gl.linkProgram(bp);
        if (gl.getProgramParameter(bp, gl.LINK_STATUS)) {
          this.bgProg = bp;
          for (const k of [
            "bgMode", "bgTop", "bgMid", "bgBot", "bgVignette", "bgBright",
            "camRight", "camUp", "camFwd", "aspect", "tanHalfFov",
            "uEnvGround", "uEnvHorizon", "uEnvZenith",
            "uKeyDir", "uKeyCol", "uFillDir", "uFillCol", "uRimDir", "uRimCol",
            "uEnvStrip", "uExposure",
          ]) this.bgLoc[k] = gl.getUniformLocation(bp, k);
        }
      } catch (_) { this.bgProg = null; }
      this.studio = {
        lightPreset: "studio_neutral",
        bgPreset: "match",
        bgCustomHex: "#1c252b",
        rotation: 0,
        exposure: 1.0,
        keyIntensity: 1.0,
        ambientIntensity: 1.0,
        shadowStrength: 0.76,
        bgBrightness: 1.0,
        autoRotate: false,
      };
      this._onStudioTick = null;
      this._bloodDecalTex = null;
      this._bloodDecalTintable = false;
      this._dummy2DTex = gl.createTexture();
      gl.bindTexture(gl.TEXTURE_2D, this._dummy2DTex);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, 1, 1, 0, gl.RGBA, gl.UNSIGNED_BYTE, new Uint8Array([0, 0, 0, 0]));
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
      // 2048x2048 hardware-PCF GPU shadow map for real-time self-shadowing.
      this.shadowSize = 2048;
      this.shadowReady = false;
      this._dummyShadowTex = gl.createTexture();
      gl.bindTexture(gl.TEXTURE_2D, this._dummyShadowTex);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.DEPTH_COMPONENT24, 1, 1, 0, gl.DEPTH_COMPONENT, gl.UNSIGNED_INT, null);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_COMPARE_MODE, gl.COMPARE_REF_TO_TEXTURE);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_COMPARE_FUNC, gl.LEQUAL);
      try {
        const sp = gl.createProgram();
        gl.attachShader(sp, compile(gl, gl.VERTEX_SHADER, SHADOW_VS));
        gl.attachShader(sp, compile(gl, gl.FRAGMENT_SHADER, SHADOW_FS));
        gl.linkProgram(sp);
        if (gl.getProgramParameter(sp, gl.LINK_STATUS)) {
          this.shadowProg = sp;
          this.shadowLocMvp = gl.getUniformLocation(sp, "lightMVP");
          this.shadowLocUseSkin = gl.getUniformLocation(sp, "useSkin");
          this.shadowLocHasSkin8 = gl.getUniformLocation(sp, "hasSkin8");
          this.shadowLocBoneTex = gl.getUniformLocation(sp, "boneTex");
          this.shadowTex = gl.createTexture();
          gl.bindTexture(gl.TEXTURE_2D, this.shadowTex);
          gl.texImage2D(gl.TEXTURE_2D, 0, gl.DEPTH_COMPONENT24, this.shadowSize, this.shadowSize, 0, gl.DEPTH_COMPONENT, gl.UNSIGNED_INT, null);
          gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
          gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
          gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
          gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
          gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_COMPARE_MODE, gl.COMPARE_REF_TO_TEXTURE);
          gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_COMPARE_FUNC, gl.LEQUAL);
          this.shadowFbo = gl.createFramebuffer();
          gl.bindFramebuffer(gl.FRAMEBUFFER, this.shadowFbo);
          gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.DEPTH_ATTACHMENT, gl.TEXTURE_2D, this.shadowTex, 0);
          gl.drawBuffers([gl.NONE]);
          gl.readBuffer(gl.NONE);
          this.shadowReady = (gl.checkFramebufferStatus(gl.FRAMEBUFFER) === gl.FRAMEBUFFER_COMPLETE);
          gl.bindFramebuffer(gl.FRAMEBUFFER, null);
        }
      } catch (_) {
        this.shadowReady = false;
        gl.bindFramebuffer(gl.FRAMEBUFFER, null);
      }
      // 3D Skeleton overlay shader (bones + joint markers, color-coded by subsystem).
      this.skelProg = null;
      try {
        const kp = gl.createProgram();
        gl.attachShader(kp, compile(gl, gl.VERTEX_SHADER, SKEL_VS));
        gl.attachShader(kp, compile(gl, gl.FRAGMENT_SHADER, SKEL_FS));
        gl.linkProgram(kp);
        if (gl.getProgramParameter(kp, gl.LINK_STATUS)) {
          this.skelProg = kp;
          this.skelLocVp = gl.getUniformLocation(kp, "vp");
          this.skelLocIsPoint = gl.getUniformLocation(kp, "isPoint");
          this._skelLineVao = gl.createVertexArray();
          this._skelLineBuf = gl.createBuffer();
          gl.bindVertexArray(this._skelLineVao);
          gl.bindBuffer(gl.ARRAY_BUFFER, this._skelLineBuf);
          gl.enableVertexAttribArray(0); gl.vertexAttribPointer(0, 3, gl.FLOAT, false, 28, 0);
          gl.enableVertexAttribArray(1); gl.vertexAttribPointer(1, 3, gl.FLOAT, false, 28, 12);
          gl.enableVertexAttribArray(2); gl.vertexAttribPointer(2, 1, gl.FLOAT, false, 28, 24);
          this._skelPtVao = gl.createVertexArray();
          this._skelPtBuf = gl.createBuffer();
          gl.bindVertexArray(this._skelPtVao);
          gl.bindBuffer(gl.ARRAY_BUFFER, this._skelPtBuf);
          gl.enableVertexAttribArray(0); gl.vertexAttribPointer(0, 3, gl.FLOAT, false, 28, 0);
          gl.enableVertexAttribArray(1); gl.vertexAttribPointer(1, 3, gl.FLOAT, false, 28, 12);
          gl.enableVertexAttribArray(2); gl.vertexAttribPointer(2, 1, gl.FLOAT, false, 28, 24);
          gl.bindVertexArray(null);
        }
      } catch (_) {
        this.skelProg = null;
      }
      // Strand Groom shader (RAGE 9 .grm polylines: hair, brows, lashes, beard, body hair)
      this.groomProg = null;
      this.groomLoc = {};
      this._grooms = new Map(); // slot -> { vao, vbo, ibo, indexCount, strandCount, rootCol, tipCol, alpha, boneName }
      try {
        const gp = gl.createProgram();
        gl.attachShader(gp, compile(gl, gl.VERTEX_SHADER, GROOM_VS));
        gl.attachShader(gp, compile(gl, gl.FRAGMENT_SHADER, GROOM_FS));
        gl.linkProgram(gp);
        if (gl.getProgramParameter(gp, gl.LINK_STATUS)) {
          this.groomProg = gp;
          for (const k of [
            "vp", "boneDelta", "standingOff", "rootCol", "tipCol", "eye", "alpha",
            "uKeyDir", "uKeyCol", "uFillDir", "uFillCol", "uRimDir", "uRimCol", "uAmbCol", "uExposure",
          ]) {
            this.groomLoc[k] = gl.getUniformLocation(gp, k);
          }
        }
      } catch (_) {
        this.groomProg = null;
      }
      // RGBA32F GPU bone palette texture (4 texels x maxBones) for 8-influence LBS.
      this._boneTexRows = 1024;
      this._boneData = new Float32Array(this._boneTexRows * 16);
      for (let i = 0; i < this._boneTexRows; i++) {
        this._boneData[i * 16] = 1; this._boneData[i * 16 + 5] = 1;
        this._boneData[i * 16 + 10] = 1; this._boneData[i * 16 + 15] = 1;
      }
      this._boneTex = gl.createTexture();
      gl.bindTexture(gl.TEXTURE_2D, this._boneTex);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA32F, 4, this._boneTexRows, 0, gl.RGBA, gl.FLOAT, this._boneData);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
      this._skel = null;
      this._skelLineCount = 0;
      this._skelPtCount = 0;
      this._anim = null;
      this._blend = null;
      this._animState = { playing: false, time: 0, speed: 1.0, loop: true, rootMotion: false };
      this._onAnimTick = null;
      // Dummy 1x1x1 TEXTURE_2D_ARRAY so sampler2DArray unit 12 is always valid on every WebGL2 driver.
      this._dummyArrayTex = gl.createTexture();
      gl.bindTexture(gl.TEXTURE_2D_ARRAY, this._dummyArrayTex);
      gl.texImage3D(gl.TEXTURE_2D_ARRAY, 0, gl.RGBA, 1, 1, 1, 0, gl.RGBA, gl.UNSIGNED_BYTE, new Uint8Array([128, 128, 128, 255]));
      gl.texParameteri(gl.TEXTURE_2D_ARRAY, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D_ARRAY, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D_ARRAY, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
      gl.texParameteri(gl.TEXTURE_2D_ARRAY, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
      this._lutArrays = new Map(); // hash -> { tex, width, height, slices }
      this._sharedLiveryTex = null;
      this.procedural = {
        vehPrimary: [0.16, 0.34, 0.56],
        vehSecondary: [0.88, 0.88, 0.89],
        vehPearlCol: [0.45, 0.72, 0.96],
        vehPaintRow1: 179,
        vehPaintRow2: 100,
        vehPaintSlice: 1,
        vehTwoTone: 0,
        vehInterior: 0,
        vehPearl: 0.35,
        vehClearcoat: 0.90,
        vehDirt: 0.0,
        vehWear: 0.0,
        vehLutExact: false,
        wpnFinish: 0,
        wpnWear: 0.25,
        liveryScale: 1.0,
      };
      this.charFx = {
        wetness: 0.0, soak: 0.0, waterHeight: 2.2, droplets: 0.55, eyeWetness: 0.2,
        dust: 0.0, dirtAmt: 0.0, dirtCol: [0.30, 0.23, 0.17], dirtRough: [0.08, 0.58],
        dirtSkinStr: 0.5, dirtClothStr: 0.7,
        underwater: 0.0, waterLine: 1.85, caustics: 0.75,
        sunTan: 0.0, sunBurn: 0.0, tanLines: 0, flush: 0.0,
        bloodSoak: 0.0,
        glints: 0.0, pedLightMult: 1.0, pedFresnel: 0.0, uvParty: 0.0,
        wounds: [], // [{pos:[x,y,z], radius, kind, intensity, drip, seed}]
      };
      this.charMorph = {
        height: 1.72, defaultHeight: 1.72,
        weight: 0.0, muscle: 0.0,
        GCBreasts: 0.0, GCCleavage: 0.0, GCButt: 0.0,
        "0xb0eae798": 0.0, gcbreastvolume: 0.0,
        gchipwidth: 0.0, gchipheight: 0.0,
        gcbreastheight: 0.0, gcchestheight: 0.0, gcbreastspacing: 0.0,
        gcmusclebiceps: 0.0, gcmuscletriceps: 0.0, gcbellyround: 0.0,
        gcweightback: 0, gcweightbelly: 0, gcweightbutt: 0, gcweightcalf: 0, gcweightchest: 0,
        gcweightforearm: 0, gcweighthip: 0, gcweightmuffin: 0, gcweightshoulder: 0,
        gcweightthigh: 0, gcweightupperarm: 0, gcweightneck: 0,
        gcmuscleback: 0, gcmusclebelly: 0, gcmusclebutt: 0, gcmusclecalf: 0, gcmusclechest: 0,
        gcmuscleforearm: 0, gcmusclehip: 0, gcmusclemuffin: 0, gcmuscleshoulder: 0,
        gcmusclethigh: 0, gcmuscleupperarm: 0, gcmuscleneck: 0,
        jiggleChest: 0.0, jiggleButt: 0.0, jiggleBelly: 0.0, jiggleStiff: 30.0, jiggleDamp: 4.2,
      };
      this._jiggle = {
        prevChest: null, prevVelChest: [0, 0, 0], dispChest: [0, 0, 0], velChest: [0, 0, 0],
        prevPelvis: null, prevVelPelvis: [0, 0, 0], dispButt: [0, 0, 0], velButt: [0, 0, 0],
        dispBelly: [0, 0, 0], velBelly: [0, 0, 0],
      };
      this.postFx = {
        active: false, intensity: 1.0, animate: true, bpm: 126, phase: 0.0,
        preset: null, liveReadout: null,
        bloom: 0, lensK1: 0, lensK2: 0, chromK1: 0, chromK2: 0,
        chromCol: [0, 0, 0], vigCol: [0, 0, 0], vigInt: 0, vigIn: 0.5, vigOut: 2.5, vigBloom: 0,
        desat: 1.0, shCon: 1.0, hlCon: 1.0, dtCon: 1.0, noise: 0, exposure: 0, trail: 0,
        redShadow: 0, uvParty: 0, glints: 0, pedLightMult: 1.0, pedFresnel: 0,
      };
      this._fxTime = 0;
      this._hiddenBoneRoots = new Set();
      this._hiddenBoneIdx = new Set();
      this._vitalOrgans = [];
      this._showVitalOrgans = false;
      this._pickMode = false;
      this._onSurfacePick = null;
      this._onPostFxTick = null;
      this.prims = [];
      this.materials = new Map();
      this._uploadQueue = [];
      this._uploadGen = 0;
      this.opts = { mode: "textured", overlay: false, normals: true, grid: true, box: false, backfaces: true,
        alpha: false, flipv: false, spin: false, skeleton: false, skelXray: true, skelFilter: "core", selectedBone: -1 };
      this.cam = { yaw: 0.7, pitch: 0.35, dist: 4, target: [0, 0, 0] };
      this.grid = null;
      this.boxMesh = null;
      this.highlightKey = null;
      this.dragging = false;
      this.lastTick = 0;
      this.bindInput();
      new ResizeObserver(() => this.draw()).observe(canvas);
    }
    _freeMat(m) {
      if (!m) return;
      for (const f of EXTRA_TEX_FIELDS) {
        if (m[f]) { this.gl.deleteTexture(m[f]); m[f] = null; }
      }
    }
    freePrims(list) {
      const gl = this.gl, vaos = new Set(), bufs = new Set();
      for (const p of list) {
        if (p.vao && !vaos.has(p.vao)) { vaos.add(p.vao); gl.deleteVertexArray(p.vao); }
        if (p.evao && !vaos.has(p.evao)) { vaos.add(p.evao); gl.deleteVertexArray(p.evao); }
        for (const b of p.buffers) if (!bufs.has(b)) { bufs.add(b); gl.deleteBuffer(b); }
      }
    }
    clear() {
      this.freePrims(this.prims);
      this.prims = [];
      this.layerBounds = new Map();
      this.bounds = null;
      this.clearSkeleton();
      this.clearGroomStrands();
      this.draw();
    }
    _parseColorVec3(c, fallback) {
      if (Array.isArray(c) && c.length >= 3) {
        const s = (c[0] > 1.01 || c[1] > 1.01 || c[2] > 1.01) ? 1 / 255 : 1;
        return [c[0] * s, c[1] * s, c[2] * s];
      }
      if (typeof c === "string" && /^#[0-9a-f]{6}$/i.test(c)) {
        return [
          parseInt(c.slice(1, 3), 16) / 255,
          parseInt(c.slice(3, 5), 16) / 255,
          parseInt(c.slice(5, 7), 16) / 255,
        ];
      }
      return fallback;
    }
    setGroomStrands(slot, arrayBuffer, colorSpec = null) {
      this.removeGroomStrands(slot, false);
      if (!this.groomProg || !arrayBuffer || arrayBuffer.byteLength < 52) {
        this.draw();
        return null;
      }
      const dv = new DataView(arrayBuffer);
      if (dv.getUint8(0) !== 83 || dv.getUint8(1) !== 84 || dv.getUint8(2) !== 82 || dv.getUint8(3) !== 49) return null;
      const vcnt = dv.getUint32(4, true);
      const icnt = dv.getUint32(8, true);
      const scnt = dv.getUint32(12, true);
      if (!vcnt || !icnt) {
        this.draw();
        return null;
      }
      const vOff = 52;
      const iOff = vOff + vcnt * 16;
      if (arrayBuffer.byteLength < iOff + icnt * 4) return null;
      const verts = new Float32Array(arrayBuffer, vOff, vcnt * 4);
      const idx = new Uint32Array(arrayBuffer, iOff, icnt);
      const gl = this.gl;
      const vao = gl.createVertexArray();
      const vbo = gl.createBuffer();
      const ibo = gl.createBuffer();
      gl.bindVertexArray(vao);
      gl.bindBuffer(gl.ARRAY_BUFFER, vbo);
      gl.bufferData(gl.ARRAY_BUFFER, verts, gl.STATIC_DRAW);
      gl.enableVertexAttribArray(0);
      gl.vertexAttribPointer(0, 4, gl.FLOAT, false, 0, 0);
      gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, ibo);
      gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, idx, gl.STATIC_DRAW);
      gl.bindVertexArray(null);
      const tipCol = this._parseColorVec3(colorSpec?.tipRgb || colorSpec?.tipHex || colorSpec?.color, [0.35, 0.25, 0.18]);
      const rootCol = this._parseColorVec3(colorSpec?.rootRgb || colorSpec?.rootHex, [tipCol[0] * 0.65, tipCol[1] * 0.65, tipCol[2] * 0.65]);
      const alpha = slot === "bodyhair" ? 0.65 : (slot === "lashes" ? 0.96 : 0.94);
      const boneName = slot === "bodyhair" ? "SKEL_Spine_Root" : "SKEL_Head";
      const rec = { vao, vbo, ibo, indexCount: icnt, strandCount: scnt, rootCol, tipCol, alpha, boneName };
      this._grooms.set(slot, rec);
      this.draw();
      return { strandCount: scnt, indexCount: icnt };
    }
    setGroomColor(colorSpec, slot = null) {
      if (!colorSpec || !this._grooms) return;
      const tipCol = this._parseColorVec3(colorSpec.tipRgb || colorSpec.tipHex || colorSpec.color, null);
      if (!tipCol) return;
      const rootCol = this._parseColorVec3(colorSpec.rootRgb || colorSpec.rootHex, [tipCol[0] * 0.65, tipCol[1] * 0.65, tipCol[2] * 0.65]);
      for (const [k, g] of this._grooms.entries()) {
        if (slot && k !== slot) continue;
        if (k === "lashes") {
          // Eyelashes stay slightly darker/natural than dyed scalp hair unless explicitly targeted
          g.rootCol = slot ? rootCol : [rootCol[0] * 0.45, rootCol[1] * 0.45, rootCol[2] * 0.45];
          g.tipCol = slot ? tipCol : [tipCol[0] * 0.55, tipCol[1] * 0.55, tipCol[2] * 0.55];
        } else {
          g.rootCol = rootCol;
          g.tipCol = tipCol;
        }
      }
      this.draw();
    }
    removeGroomStrands(slot, redraw = true) {
      if (!this._grooms) return;
      const g = this._grooms.get(slot);
      if (g) {
        const gl = this.gl;
        if (g.vao) gl.deleteVertexArray(g.vao);
        if (g.vbo) gl.deleteBuffer(g.vbo);
        if (g.ibo) gl.deleteBuffer(g.ibo);
        this._grooms.delete(slot);
        if (redraw) this.draw();
      }
    }
    clearGroomStrands() {
      if (!this._grooms) return;
      for (const k of [...this._grooms.keys()]) this.removeGroomStrands(k, false);
    }
    removeLayer(layer) {
      this.freePrims(this.prims.filter((p) => p.layer === layer));
      this.prims = this.prims.filter((p) => p.layer !== layer);
      this.layerBounds?.delete(layer);
      const prefix = layer + ":";
      this._uploadQueue = this._uploadQueue.filter((u) => !u.key.startsWith(prefix));
      for (const k of [...this.materials.keys()]) if (k.startsWith(prefix)) {
        this._freeMat(this.materials.get(k));
        this.materials.delete(k);
      }
      this.unionBounds();
      this.draw();
    }
    unionBounds() {
      const lo = [Infinity, Infinity, Infinity], hi = [-Infinity, -Infinity, -Infinity];
      for (const [l, h] of this.layerBounds?.values() || []) for (let k = 0; k < 3; k++) { lo[k] = Math.min(lo[k], l[k]); hi[k] = Math.max(hi[k], h[k]); }
      this.bounds = isFinite(lo[0]) ? [lo, hi] : null;
      if (this.bounds) { this.buildGrid(); this.buildBox(); }
    }
    // Load a GLB as the only layer ("main"), or add/replace a layer of an assembly (optionally instanced by 4x4 matrices).
    load(arrayBuffer, layer = "main", keep = false, keepCam = false, instances = null) {
      if (!keep) {
        this.freePrims(this.prims);
        this.prims = [];
        this.layerBounds = new Map();
      } else {
        this.freePrims(this.prims.filter((p) => p.layer === layer));
        this.prims = this.prims.filter((p) => p.layer !== layer);
        this.layerBounds?.delete(layer);
      }
      const gl = this.gl, { doc, bin } = parseGlb(arrayBuffer);
      const lo = [Infinity, Infinity, Infinity], hi = [-Infinity, -Infinity, -Infinity];
      let tris = 0, verts = 0, primIndex = 0;
      const instList = instances && instances.length ? instances : [null];
      const nodes = doc.scenes[doc.scene || 0].nodes.map((i) => doc.nodes[i]);
      for (const node of nodes) {
        if (node.mesh == null) continue;
        const baseModel = nodeMatrix(node);
        for (const prim of doc.meshes[node.mesh].primitives) {
          const curPrimIdx = primIndex++;
          const pos = accessor(doc, bin, prim.attributes.POSITION);
          const nrm = prim.attributes.NORMAL != null ? accessor(doc, bin, prim.attributes.NORMAL) : null;
          const uv = prim.attributes.TEXCOORD_0 != null ? accessor(doc, bin, prim.attributes.TEXCOORD_0) : null;
          const uv1 = prim.attributes.TEXCOORD_1 != null ? accessor(doc, bin, prim.attributes.TEXCOORD_1) : null;
          const col = prim.attributes.COLOR_0 != null ? accessor(doc, bin, prim.attributes.COLOR_0) : null;
          const blendW = prim.attributes.COLOR_1 != null ? accessor(doc, bin, prim.attributes.COLOR_1) : null;
          const rawIdx = accessor(doc, bin, prim.indices);
          const nv = pos.length / 3;
          // Compact index buffer to 16-bit whenever vertex count fits (halves index VRAM).
          const idx = (rawIdx instanceof Uint32Array && nv <= 65536) ? new Uint16Array(rawIdx) : rawIdx;
          const vao = gl.createVertexArray(), buffers = [];
          gl.bindVertexArray(vao);
          const attr = (loc, data, size) => {
            const b = gl.createBuffer(); buffers.push(b);
            gl.bindBuffer(gl.ARRAY_BUFFER, b); gl.bufferData(gl.ARRAY_BUFFER, data, gl.STATIC_DRAW);
            gl.enableVertexAttribArray(loc); gl.vertexAttribPointer(loc, size, gl.FLOAT, false, 0, 0);
          };
          attr(0, pos, 3);
          if (nrm) {
            // Quantize normals to normalized signed bytes (4 bytes/vert aligned vs 12 bytes float32).
            const packed = new Int8Array(nv * 4);
            for (let i = 0, j = 0; i < nrm.length; i += 3, j += 4) {
              packed[j] = Math.max(-127, Math.min(127, Math.round(nrm[i] * 127)));
              packed[j + 1] = Math.max(-127, Math.min(127, Math.round(nrm[i + 1] * 127)));
              packed[j + 2] = Math.max(-127, Math.min(127, Math.round(nrm[i + 2] * 127)));
            }
            const nb = gl.createBuffer(); buffers.push(nb);
            gl.bindBuffer(gl.ARRAY_BUFFER, nb); gl.bufferData(gl.ARRAY_BUFFER, packed, gl.STATIC_DRAW);
            gl.enableVertexAttribArray(1); gl.vertexAttribPointer(1, 3, gl.BYTE, true, 4, 0);
          } else gl.disableVertexAttribArray(1);
          if (uv) attr(2, uv, 2); else gl.disableVertexAttribArray(2);
          if (col) attr(3, col, 4); else { gl.disableVertexAttribArray(3); gl.vertexAttrib4f(3, 1, 1, 1, 1); }
          if (uv1) attr(4, uv1, 2); else { gl.disableVertexAttribArray(4); gl.vertexAttrib2f(4, 0, 0); }
          if (blendW) attr(5, blendW, 4); else { gl.disableVertexAttribArray(5); gl.vertexAttrib4f(5, 0, 0, 0, 0); }
          gl.disableVertexAttribArray(6); gl.vertexAttrib4f(6, 0, 0, 0, 0);
          gl.disableVertexAttribArray(7); gl.vertexAttrib4f(7, 1, 0, 0, 0);
          gl.disableVertexAttribArray(8); gl.vertexAttrib4f(8, 0, 0, 0, 0);
          gl.disableVertexAttribArray(9); gl.vertexAttrib4f(9, 0, 0, 0, 0);
          const ib = gl.createBuffer(); buffers.push(ib);
          gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, ib); gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, idx, gl.STATIC_DRAW);
          // Unique triangle edges for wireframe drawing.
          const seen = new Set(), edges = [];
          for (let t = 0; t < idx.length; t += 3) for (const [a, b] of [[idx[t], idx[t + 1]], [idx[t + 1], idx[t + 2]], [idx[t + 2], idx[t]]]) {
            const k = a < b ? a * nv + b : b * nv + a;
            if (!seen.has(k)) { seen.add(k); edges.push(a, b); }
          }
          const evao = gl.createVertexArray();
          gl.bindVertexArray(evao);
          gl.bindBuffer(gl.ARRAY_BUFFER, buffers[0]); gl.enableVertexAttribArray(0); gl.vertexAttribPointer(0, 3, gl.FLOAT, false, 0, 0);
          gl.disableVertexAttribArray(6); gl.vertexAttrib4f(6, 0, 0, 0, 0);
          gl.disableVertexAttribArray(7); gl.vertexAttrib4f(7, 1, 0, 0, 0);
          gl.disableVertexAttribArray(8); gl.vertexAttrib4f(8, 0, 0, 0, 0);
          gl.disableVertexAttribArray(9); gl.vertexAttrib4f(9, 0, 0, 0, 0);
          const eb = gl.createBuffer(); buffers.push(eb);
          const edgeData = nv > 65536 ? new Uint32Array(edges) : new Uint16Array(edges);
          gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, eb); gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, edgeData, gl.STATIC_DRAW);
          gl.bindVertexArray(null);
          const ext = prim.extras || {};
          const hasColorRGB = ext.hasColorRGB != null ? !!ext.hasColorRGB : (!!col && !uv);
          const hasTintIndex = ext.hasTintIndex != null ? !!ext.hasTintIndex : !!col;
          const hasVertAlpha = !!ext.hasVertexAlpha;
          for (const instMat of instList) {
            const model = instMat ? mat.mul(instMat, baseModel) : baseModel;
            for (let i = 0; i < pos.length; i += 3) {
              const px = pos[i], py = pos[i + 1], pz = pos[i + 2];
              const wx = model[0] * px + model[4] * py + model[8] * pz + model[12];
              const wy = model[1] * px + model[5] * py + model[9] * pz + model[13];
              const wz = model[2] * px + model[6] * py + model[10] * pz + model[14];
              if (wx < lo[0]) lo[0] = wx; if (wx > hi[0]) hi[0] = wx;
              if (wy < lo[1]) lo[1] = wy; if (wy > hi[1]) hi[1] = wy;
              if (wz < lo[2]) lo[2] = wz; if (wz > hi[2]) hi[2] = wz;
            }
            tris += idx.length / 3; verts += nv;
            const keepCpu = nv <= 25000 && instList.length === 1;
            this.prims.push({ layer, primIndex: curPrimIdx, nv, skinned: false, hasSkin8: false,
              vao, evao, buffers, ib, eb, count: idx.length, ecount: edges.length, model,
              _rawPos: keepCpu ? pos : null, _rawIdx: keepCpu ? idx : null, _rawUV: keepCpu ? uv : null,
              _origCount: idx.length, _origEdgeData: keepCpu ? edgeData : null,
              itype: idx instanceof Uint32Array ? gl.UNSIGNED_INT : gl.UNSIGNED_SHORT,
              etype: edgeData instanceof Uint32Array ? gl.UNSIGNED_INT : gl.UNSIGNED_SHORT,
              shader: ext.shaderIndex ?? 0, hasNormals: !!nrm, hasUV: !!uv, hasUV1: !!uv1,
              hasColor: hasColorRGB, hasTintIndex, hasVertAlpha, hasBlendW: !!blendW, tris: idx.length / 3 });
          }
        }
      }
      if (!this.layerBounds) this.layerBounds = new Map();
      this.layerBounds.set(layer, [lo, hi]);
      const mine = this.prims.filter((p) => p.layer === layer);
      const info = { primitives: mine.length, triangles: tris, vertices: verts, extras: doc.extras || {},
        atRootStub: !!(doc.extras && doc.extras.atRootStub),
        withoutUV: mine.filter((p) => !p.hasUV).length, withColor: mine.filter((p) => p.hasColor).length,
        withUV1: mine.filter((p) => p.hasUV1).length };
      this.unionBounds();
      if (!keep && !keepCam) this.fit(); else this.draw();
      return info;
    }
    // Remove coplanar triangles from targetLayer that overlap any reference layer within tol (meters),
    // optionally filtered by TEXCOORD_0.v bounds (vMin/vMax) to preserve each mesh's native UV atlas
    // across RAGE 9's waist/nipple/genital body-cull seams without Z-fighting or cross-atlas artifacts.
    cullOverlap(targetLayer, refSpecs) {
      const gl = this.gl;
      const targets = this.prims.filter((p) => p.layer === targetLayer && p._rawPos && p._rawIdx && p.ib);
      if (!targets.length) return 0;
      const activeSpecs = [];
      for (const spec of refSpecs || []) {
        const rprims = this.prims.filter((p) => p.layer === spec.layer && p._rawPos && p._rawIdx);
        if (!rprims.length) continue;
        const tol = Math.max(0.0005, spec.tol || 0.004);
        const inv = 1.0 / tol;
        const grid = new Map();
        for (const rp of rprims) {
          const pos = rp._rawPos, idx = rp._rawIdx, m = rp.model;
          for (let t = 0; t < idx.length; t += 3) {
            const a = idx[t] * 3, b = idx[t + 1] * 3, c = idx[t + 2] * 3;
            const px = (pos[a] + pos[b] + pos[c]) / 3;
            const py = (pos[a + 1] + pos[b + 1] + pos[c + 1]) / 3;
            const pz = (pos[a + 2] + pos[b + 2] + pos[c + 2]) / 3;
            const wx = m[0] * px + m[4] * py + m[8] * pz + m[12];
            const wy = m[1] * px + m[5] * py + m[9] * pz + m[13];
            const wz = m[2] * px + m[6] * py + m[10] * pz + m[14];
            const gx = Math.floor(wx * inv), gy = Math.floor(wy * inv), gz = Math.floor(wz * inv);
            const key = `${gx},${gy},${gz}`;
            let bucket = grid.get(key);
            if (!bucket) { bucket = []; grid.set(key, bucket); }
            bucket.push(wx, wy, wz);
          }
        }
        activeSpecs.push({
          grid, tol2: tol * tol, inv,
          vMin: spec.vMin != null ? spec.vMin : null,
          vMax: spec.vMax != null ? spec.vMax : null,
        });
      }
      const uploadEdges = (tp, idxSlice, isOrig) => {
        if (!tp.eb || !tp.evao) return;
        let edgeData;
        if (isOrig && tp._origEdgeData) {
          edgeData = tp._origEdgeData;
        } else {
          const nv = tp.nv, seen = new Set(), edges = [];
          for (let t = 0; t < idxSlice.length; t += 3) {
            const i0 = idxSlice[t], i1 = idxSlice[t + 1], i2 = idxSlice[t + 2];
            for (const [a, b] of [[i0, i1], [i1, i2], [i2, i0]]) {
              const k = a < b ? a * nv + b : b * nv + a;
              if (!seen.has(k)) { seen.add(k); edges.push(a, b); }
            }
          }
          edgeData = nv > 65536 ? new Uint32Array(edges) : new Uint16Array(edges);
        }
        gl.bindVertexArray(tp.evao);
        gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, tp.eb);
        gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, edgeData, gl.STATIC_DRAW);
        gl.bindVertexArray(null);
        tp.ecount = edgeData.length;
      };
      let culledTotal = 0;
      for (const tp of targets) {
        const rawIdx = tp._rawIdx;
        if (!activeSpecs.length) {
          if (tp.count !== tp._origCount) {
            gl.bindVertexArray(tp.vao);
            gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, tp.ib);
            gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, rawIdx, gl.STATIC_DRAW);
            gl.bindVertexArray(null);
            tp.count = tp._origCount;
            tp.tris = tp._origCount / 3;
            uploadEdges(tp, rawIdx, true);
          }
          continue;
        }
        const pos = tp._rawPos, uv = tp._rawUV, m = tp.model;
        const out = new (rawIdx instanceof Uint32Array ? Uint32Array : Uint16Array)(rawIdx.length);
        let outLen = 0;
        for (let t = 0; t < rawIdx.length; t += 3) {
          const ia = rawIdx[t], ib_ = rawIdx[t + 1], ic = rawIdx[t + 2];
          const a = ia * 3, b = ib_ * 3, c = ic * 3;
          const px = (pos[a] + pos[b] + pos[c]) / 3;
          const py = (pos[a + 1] + pos[b + 1] + pos[c + 1]) / 3;
          const pz = (pos[a + 2] + pos[b + 2] + pos[c + 2]) / 3;
          const wx = m[0] * px + m[4] * py + m[8] * pz + m[12];
          const wy = m[1] * px + m[5] * py + m[9] * pz + m[13];
          const wz = m[2] * px + m[6] * py + m[10] * pz + m[14];
          const tv = uv ? (uv[ia * 2 + 1] + uv[ib_ * 2 + 1] + uv[ic * 2 + 1]) / 3 : null;
          let drop = false;
          for (const sp of activeSpecs) {
            if (sp.vMin != null && (tv === null || tv < sp.vMin)) continue;
            if (sp.vMax != null && (tv === null || tv > sp.vMax)) continue;
            const gx = Math.floor(wx * sp.inv), gy = Math.floor(wy * sp.inv), gz = Math.floor(wz * sp.inv);
            for (let dx = -1; dx <= 1 && !drop; dx++) {
              for (let dy = -1; dy <= 1 && !drop; dy++) {
                for (let dz = -1; dz <= 1 && !drop; dz++) {
                  const bucket = sp.grid.get(`${gx + dx},${gy + dy},${gz + dz}`);
                  if (!bucket) continue;
                  for (let k = 0; k < bucket.length; k += 3) {
                    const ex = wx - bucket[k], ey = wy - bucket[k + 1], ez = wz - bucket[k + 2];
                    if (ex * ex + ey * ey + ez * ez <= sp.tol2) { drop = true; break; }
                  }
                }
              }
            }
            if (drop) break;
          }
          if (drop) {
            culledTotal++;
          } else {
            out[outLen++] = ia;
            out[outLen++] = ib_;
            out[outLen++] = ic;
          }
        }
        const slice = out.subarray(0, outLen);
        gl.bindVertexArray(tp.vao);
        gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, tp.ib);
        gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, slice, gl.STATIC_DRAW);
        gl.bindVertexArray(null);
        tp.count = outLen;
        tp.tris = outLen / 3;
        uploadEdges(tp, slice, outLen === tp._origCount);
      }
      this.draw();
      return culledTotal;
    }
    buildGrid() {
      const gl = this.gl, [lo, hi] = this.bounds;
      const size = Math.max(hi[0] - lo[0], hi[2] - lo[2], 0.5) * 1.6;
      const step = Math.pow(10, Math.floor(Math.log10(size / 4)));
      const n = Math.ceil(size / step / 2), y = lo[1], cx = (lo[0] + hi[0]) / 2, cz = (lo[2] + hi[2]) / 2;
      const ox = Math.round(cx / step) * step, oz = Math.round(cz / step) * step;
      const v = [];
      for (let i = -n; i <= n; i++) {
        v.push(ox + i * step, y, oz - n * step, ox + i * step, y, oz + n * step);
        v.push(ox - n * step, y, oz + i * step, ox + n * step, y, oz + i * step);
      }
      if (this.grid) { gl.deleteVertexArray(this.grid.vao); gl.deleteBuffer(this.grid.buf); }
      const vao = gl.createVertexArray(), buf = gl.createBuffer();
      gl.bindVertexArray(vao); gl.bindBuffer(gl.ARRAY_BUFFER, buf);
      gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(v), gl.STATIC_DRAW);
      gl.enableVertexAttribArray(0); gl.vertexAttribPointer(0, 3, gl.FLOAT, false, 0, 0);
      gl.bindVertexArray(null);
      this.grid = { vao, buf, count: v.length / 3, step };
    }
    buildBox() {
      const gl = this.gl, [l, h] = this.bounds;
      const c = [
        [l[0], l[1], l[2]], [h[0], l[1], l[2]], [h[0], h[1], l[2]], [l[0], h[1], l[2]],
        [l[0], l[1], h[2]], [h[0], l[1], h[2]], [h[0], h[1], h[2]], [l[0], h[1], h[2]],
      ];
      const e = [0, 1, 1, 2, 2, 3, 3, 0, 4, 5, 5, 6, 6, 7, 7, 4, 0, 4, 1, 5, 2, 6, 3, 7];
      const v = [];
      for (const i of e) v.push(...c[i]);
      if (this.boxMesh) { gl.deleteVertexArray(this.boxMesh.vao); gl.deleteBuffer(this.boxMesh.buf); }
      const vao = gl.createVertexArray(), buf = gl.createBuffer();
      gl.bindVertexArray(vao); gl.bindBuffer(gl.ARRAY_BUFFER, buf);
      gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(v), gl.STATIC_DRAW);
      gl.enableVertexAttribArray(0); gl.vertexAttribPointer(0, 3, gl.FLOAT, false, 0, 0);
      gl.bindVertexArray(null);
      this.boxMesh = { vao, buf, count: v.length / 3 };
    }
    _uploadTexture(image, nearest = false) {
      const gl = this.gl, t = gl.createTexture();
      gl.bindTexture(gl.TEXTURE_2D, t);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, image);
      if (nearest) {
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
      } else {
        gl.generateMipmap(gl.TEXTURE_2D);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR_MIPMAP_LINEAR);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.REPEAT);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.REPEAT);
        if (this.extAniso && this.maxAniso > 1) {
          gl.texParameterf(gl.TEXTURE_2D, this.extAniso.TEXTURE_MAX_ANISOTROPY_EXT, this.maxAniso);
        }
      }
      return t;
    }
    _drainUploads(budgetMs = 6.0) {
      if (!this._uploadQueue.length) return;
      const gl = this.gl, t0 = performance.now();
      while (this._uploadQueue.length) {
        const job = this._uploadQueue.shift();
        if (job.gen !== this._uploadGen) continue;
        const m = this.materials.get(job.key) || { visible: true, color: [0.7, 0.7, 0.7], texture: null, normalTexture: null };
        if (m[job.field]) gl.deleteTexture(m[job.field]);
        const isTintPal = job.field === "tintPalTex";
        m[job.field] = job.image ? this._uploadTexture(job.image, isTintPal) : null;
        if (job.field === "maskTex") m.useBilinearMask = !!job.isBilinearMask;
        if (isTintPal) m.tintPalWidth = job.image ? (job.image.width || 0) : 0;
        this.materials.set(job.key, m);
        if (performance.now() - t0 >= budgetMs) break;
      }
    }
    setMaterial(index, state, layer = "main") {
      const key = layer + ":" + index;
      const m = this.materials.get(key) || { visible: true, color: [0.7, 0.7, 0.7], texture: null, normalTexture: null };
      Object.assign(m, state);
      this.materials.set(key, m);
      this.draw();
    }
    setTexture(index, image, layer = "main") {
      this.setExtraTexture(index, "texture", image, layer);
    }
    setNormalTexture(index, image, layer = "main") {
      this.setExtraTexture(index, "normalTexture", image, layer);
    }
    setExtraTexture(index, field, image, layer = "main") {
      const key = layer + ":" + index;
      // Supersede any pending queued upload for the same material/field.
      const prevIdx = this._uploadQueue.findIndex((u) => u.key === key && u.field === field);
      if (prevIdx >= 0) this._uploadQueue.splice(prevIdx, 1);
      if (!image) {
        const m = this.materials.get(key);
        if (m && m[field]) { this.gl.deleteTexture(m[field]); m[field] = null; }
        if (m && field === "maskTex") m.useBilinearMask = false;
        this.draw();
        return;
      }
      const isBilinearMask = field === "maskTex" ? analyzeImage(image).isBilinearMask : false;
      this._uploadQueue.push({ key, field, image, isBilinearMask, gen: this._uploadGen });
      this.draw();
    }
    clearExtraTextures(index, layer = "main") {
      const key = layer + ":" + index;
      const fields = new Set(["layer1Tex", "layer2Tex", "layer3Tex", "maskTex", "tintTex"]);
      this._uploadQueue = this._uploadQueue.filter((u) => !(u.key === key && fields.has(u.field)));
      const m = this.materials.get(key);
      if (!m) return;
      for (const f of fields) {
        if (m[f]) { this.gl.deleteTexture(m[f]); m[f] = null; }
      }
      m.useBilinearMask = false;
      this.draw();
    }
    setHighlight(layer, index) {
      this.highlightKey = index == null ? null : layer + ":" + index;
      this.draw();
    }
    resetMaterials() {
      this._uploadGen++;
      this._uploadQueue.length = 0;
      for (const m of this.materials.values()) this._freeMat(m);
      this.materials.clear();
      this.highlightKey = null;
    }
    set(name, value) {
      this.opts[name] = value;
      if (name === "spin" && value) this.lastTick = performance.now();
      if ((name === "skeleton" || name === "skelFilter" || name === "selectedBone" || name === "skelXray") && this._skel) {
        this._rebuildSkelOverlay();
      }
      this.draw();
    }
    _ensureBoneTexCapacity(needed) {
      if (needed <= this._boneTexRows) return;
      const gl = this.gl;
      let rows = this._boneTexRows;
      while (rows < needed) rows *= 2;
      rows = Math.min(4096, rows);
      const data = new Float32Array(rows * 16);
      data.set(this._boneData.subarray(0, Math.min(this._boneData.length, data.length)));
      for (let i = this._boneTexRows; i < rows; i++) {
        data[i * 16] = 1; data[i * 16 + 5] = 1; data[i * 16 + 10] = 1; data[i * 16 + 15] = 1;
      }
      this._boneTexRows = rows;
      this._boneData = data;
      gl.bindTexture(gl.TEXTURE_2D, this._boneTex);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA32F, 4, rows, 0, gl.RGBA, gl.FLOAT, data);
    }
    clearSkeleton() {
      this._skel = null;
      this._anim = null;
      this._blend = null;
      this._animState.playing = false;
      this._animState.time = 0;
      this._skelLineCount = 0;
      this._skelPtCount = 0;
      this.opts.selectedBone = -1;
      for (const p of this.prims) { p.skinned = false; p.hasSkin8 = false; }
      const d = this._boneData;
      d.fill(0);
      for (let i = 0; i < this._boneTexRows; i++) {
        d[i * 16] = 1; d[i * 16 + 5] = 1; d[i * 16 + 10] = 1; d[i * 16 + 15] = 1;
      }
      const gl = this.gl;
      gl.bindTexture(gl.TEXTURE_2D, this._boneTex);
      gl.texSubImage2D(gl.TEXTURE_2D, 0, 0, 0, 4, Math.min(this._boneTexRows, 1024), gl.RGBA, gl.FLOAT, d);
      if (this._onAnimTick) this._onAnimTick(this._animState, null);
      this.draw();
    }
    setSkeleton(skelData) {
      if (!skelData || !skelData.joints || !skelData.joints.length) {
        this.clearSkeleton();
        return;
      }
      const joints = skelData.joints.map((j) => ({
        ...j,
        _ibm: new Float32Array(j.ibmCol),
      }));
      this._skel = {
        ...skelData,
        masterCount: joints.length,
        joints,
        extraByHash: new Map(),
      };
      this._anim = null;
      this._blend = null;
      this._animState.playing = false;
      this._animState.time = 0;
      this.opts.selectedBone = -1;
      this._jiggle.prevChest = null;
      this._jiggle.prevPelvis = null;
      this._rebuildHiddenBoneSet();
      this._ensureBoneTexCapacity(joints.length + 128);
      this._evaluatePose();
      if (this._onAnimTick) this._onAnimTick(this._animState, null);
      this.draw();
    }
    getSkeletonJoints() {
      return this._skel ? this._skel.joints : [];
    }
    attachSkin(layer, arrayBuffer) {
      if (!this._skel || !arrayBuffer || arrayBuffer.byteLength < 8) return null;
      const dv = new DataView(arrayBuffer);
      const hlen = dv.getUint32(0, true);
      if (4 + hlen > arrayBuffer.byteLength) return null;
      const header = JSON.parse(new TextDecoder().decode(new Uint8Array(arrayBuffer, 4, hlen)));
      const baseOff = 4 + hlen;
      const masterCount = header.masterBoneCount || this._skel.masterCount;
      const extraJoints = header.extraJoints || [];
      const extraRemap = new Uint16Array(extraJoints.length);
      let extraAdded = 0;
      for (let k = 0; k < extraJoints.length; k++) {
        const ej = extraJoints[k];
        const existing = this._skel.extraByHash.get(ej.hash);
        if (existing != null) {
          extraRemap[k] = existing;
          continue;
        }
        const newIdx = this._skel.joints.length;
        let par = ej.parent;
        if (par >= masterCount) {
          const pk = par - masterCount;
          par = pk >= 0 && pk < k ? extraRemap[pk] : 0;
        }
        this._skel.joints.push({
          ...ej,
          index: newIdx,
          parent: par,
          _ibm: new Float32Array(ej.ibmCol),
        });
        this._skel.extraByHash.set(ej.hash, newIdx);
        extraRemap[k] = newIdx;
        extraAdded++;
      }
      if (extraAdded > 0) this._ensureBoneTexCapacity(this._skel.joints.length);

      const gl = this.gl;
      const layerPrims = this.prims.filter((p) => p.layer === layer);
      const remapIndices = (src) => {
        if (!extraJoints.length) return src;
        const out = new Uint16Array(src.length);
        for (let i = 0; i < src.length; i++) {
          const v = src[i];
          out[i] = v >= masterCount ? (extraRemap[v - masterCount] || 0) : v;
        }
        return out;
      };
      let skinnedPrims = 0;
      for (const sp of header.primitives || []) {
        if (!sp.skinned) continue;
        const nv = sp.vertices;
        const matches = layerPrims.filter((p) => p.primIndex === sp.primIndex && p.nv === nv);
        if (!matches.length) continue;
        const bytes4 = nv * 8;
        const off0 = baseOff + sp.offset;
        if (off0 + bytes4 * (sp.hasSkin8 ? 4 : 2) > arrayBuffer.byteLength) continue;
        const j0 = remapIndices(new Uint16Array(arrayBuffer, off0, nv * 4));
        const w0 = new Uint16Array(arrayBuffer, off0 + bytes4, nv * 4);
        const j1 = sp.hasSkin8 ? remapIndices(new Uint16Array(arrayBuffer, off0 + bytes4 * 2, nv * 4)) : null;
        const w1 = sp.hasSkin8 ? new Uint16Array(arrayBuffer, off0 + bytes4 * 3, nv * 4) : null;
        for (const p of matches) {
          const bJ0 = gl.createBuffer(), bW0 = gl.createBuffer();
          p.buffers.push(bJ0, bW0);
          gl.bindBuffer(gl.ARRAY_BUFFER, bJ0); gl.bufferData(gl.ARRAY_BUFFER, j0, gl.STATIC_DRAW);
          gl.bindBuffer(gl.ARRAY_BUFFER, bW0); gl.bufferData(gl.ARRAY_BUFFER, w0, gl.STATIC_DRAW);
          let bJ1 = null, bW1 = null;
          if (sp.hasSkin8 && j1 && w1) {
            bJ1 = gl.createBuffer(); bW1 = gl.createBuffer();
            p.buffers.push(bJ1, bW1);
            gl.bindBuffer(gl.ARRAY_BUFFER, bJ1); gl.bufferData(gl.ARRAY_BUFFER, j1, gl.STATIC_DRAW);
            gl.bindBuffer(gl.ARRAY_BUFFER, bW1); gl.bufferData(gl.ARRAY_BUFFER, w1, gl.STATIC_DRAW);
          }
          for (const targetVao of [p.vao, p.evao]) {
            if (!targetVao) continue;
            gl.bindVertexArray(targetVao);
            gl.bindBuffer(gl.ARRAY_BUFFER, bJ0); gl.enableVertexAttribArray(6); gl.vertexAttribPointer(6, 4, gl.UNSIGNED_SHORT, false, 0, 0);
            gl.bindBuffer(gl.ARRAY_BUFFER, bW0); gl.enableVertexAttribArray(7); gl.vertexAttribPointer(7, 4, gl.UNSIGNED_SHORT, true, 0, 0);
            if (bJ1 && bW1) {
              gl.bindBuffer(gl.ARRAY_BUFFER, bJ1); gl.enableVertexAttribArray(8); gl.vertexAttribPointer(8, 4, gl.UNSIGNED_SHORT, false, 0, 0);
              gl.bindBuffer(gl.ARRAY_BUFFER, bW1); gl.enableVertexAttribArray(9); gl.vertexAttribPointer(9, 4, gl.UNSIGNED_SHORT, true, 0, 0);
            } else {
              gl.disableVertexAttribArray(8); gl.vertexAttrib4f(8, 0, 0, 0, 0);
              gl.disableVertexAttribArray(9); gl.vertexAttrib4f(9, 0, 0, 0, 0);
            }
          }
          gl.bindVertexArray(null);
          p.skinned = true;
          p.hasSkin8 = !!sp.hasSkin8;
          skinnedPrims++;
        }
      }
      this._evaluatePose();
      this.draw();
      return {
        totalJoints: this._skel.joints.length,
        masterJoints: this._skel.masterCount,
        extraJointsTotal: this._skel.joints.length - this._skel.masterCount,
        extraAdded,
        localBones: header.localBones || 0,
        maxInfluences: header.maxInfluences || 4,
        skinnedPrims,
      };
    }
    _parseAnimBuffer(arrayBuffer) {
      if (!arrayBuffer || arrayBuffer.byteLength < 8) return null;
      const dv = new DataView(arrayBuffer);
      const hlen = dv.getUint32(0, true);
      if (4 + hlen > arrayBuffer.byteLength) return null;
      const header = JSON.parse(new TextDecoder().decode(new Uint8Array(arrayBuffer, 4, hlen)));
      const baseOff = 4 + hlen;
      const frames = Math.max(1, header.frames | 0);
      const rot = new Map(), trans = new Map(), scale = new Map();
      for (const [jidx, off] of header.rot || []) rot.set(jidx, new Float32Array(arrayBuffer, baseOff + off, frames * 4));
      for (const [jidx, off] of header.trans || []) trans.set(jidx, new Float32Array(arrayBuffer, baseOff + off, frames * 3));
      for (const [jidx, off] of header.scale || []) scale.set(jidx, new Float32Array(arrayBuffer, baseOff + off, frames * 3));
      const moverPos = header.moverPosOffset >= 0 ? new Float32Array(arrayBuffer, baseOff + header.moverPosOffset, frames * 3) : null;
      let moverRot = null;
      if (header.moverRotOffset >= 0) {
        const raw = new Float32Array(arrayBuffer, baseOff + header.moverRotOffset, frames * 4);
        // Rebase mover rotation relative to frame 0 so enabling root motion preserves facing direction at t=0.
        const q0x = -raw[0], q0y = -raw[1], q0z = -raw[2], q0w = raw[3];
        const l0 = Math.hypot(q0x, q0y, q0z, q0w) || 1;
        const ix = q0x / l0, iy = q0y / l0, iz = q0z / l0, iw = q0w / l0;
        moverRot = new Float32Array(frames * 4);
        for (let f = 0; f < frames; f++) {
          const ax = raw[f * 4], ay = raw[f * 4 + 1], az = raw[f * 4 + 2], aw = raw[f * 4 + 3];
          moverRot[f * 4]     = aw * ix + ax * iw + ay * iz - az * iy;
          moverRot[f * 4 + 1] = aw * iy - ax * iz + ay * iw + az * ix;
          moverRot[f * 4 + 2] = aw * iz + ax * iy - ay * ix + az * iw;
          moverRot[f * 4 + 3] = aw * iw - ax * ix - ay * iy - az * iz;
        }
      }
      return { ...header, frames, rot, trans, scale, moverPos, moverRot };
    }
    setAnimation(arrayBuffer) {
      this._blend = null;
      const parsed = this._parseAnimBuffer(arrayBuffer);
      if (!parsed) {
        this._anim = null;
        this._animState.playing = false;
        this._animState.time = 0;
        if (this._skel) this._evaluatePose();
        if (this._onAnimTick) this._onAnimTick(this._animState, null);
        this.draw();
        return null;
      }
      this._anim = parsed;
      this._animState.time = 0;
      this._animState.playing = parsed.frames > 1;
      this.lastTick = performance.now();
      if (this._skel) this._evaluatePose();
      if (this._onAnimTick) this._onAnimTick(this._animState, this._anim);
      this.draw();
      return parsed;
    }
    setBlendspace(entries, meta = null) {
      this._anim = null;
      const clips = [];
      const weights = [];
      for (const e of entries || []) {
        const parsed = e && e.buffer ? this._parseAnimBuffer(e.buffer) : null;
        if (!parsed) continue;
        clips.push({
          ...parsed,
          childIndex: e.childIndex ?? clips.length,
          childDuration: e.childDuration > 0 ? e.childDuration : (parsed.duration || 1.0),
          blendDuration: e.blendDuration > 0 ? e.blendDuration : (parsed.duration || 1.0),
          timeCurve: Array.isArray(e.timeCurve) && e.timeCurve.length >= 2 ? e.timeCurve : null,
        });
        weights.push(Math.max(0, Number(e.weight) || 0));
      }
      if (!clips.length) {
        this._blend = null;
        this._animState.playing = false;
        this._animState.time = 0;
        if (this._skel) this._evaluatePose();
        if (this._onAnimTick) this._onAnimTick(this._animState, null);
        this.draw();
        return null;
      }
      const wArr = new Float32Array(weights);
      let wSum = 0;
      for (let i = 0; i < wArr.length; i++) wSum += wArr[i];
      if (wSum <= 1e-6) { wArr[0] = 1; wSum = 1; }
      let dur = 0;
      for (let i = 0; i < wArr.length; i++) {
        wArr[i] /= wSum;
        dur += wArr[i] * (clips[i].blendDuration || clips[i].duration || 1.0);
      }
      this._blend = {
        name: (meta && meta.name) || "blendspace",
        clips,
        weights: wArr,
        duration: Math.max(0.05, dur),
        frames: Math.max(...clips.map((c) => c.frames || 1)),
        isBlendspace: true,
      };
      this._animState.time = 0;
      this._animState.playing = true;
      this.lastTick = performance.now();
      if (this._skel) this._evaluatePose();
      if (this._onAnimTick) this._onAnimTick(this._animState, this._blend);
      this.draw();
      return this._blend;
    }
    setBlendWeights(weightsByChildIndex) {
      const b = this._blend;
      if (!b || !b.clips.length) return;
      const oldDur = b.duration || 1.0;
      const phase = oldDur > 0 ? ((this._animState.time / oldDur) % 1 + 1) % 1 : 0;
      let wSum = 0;
      for (let i = 0; i < b.clips.length; i++) {
        const ci = b.clips[i].childIndex;
        const w = Array.isArray(weightsByChildIndex)
          ? Math.max(0, Number(weightsByChildIndex[ci] ?? weightsByChildIndex[i]) || 0)
          : Math.max(0, Number(weightsByChildIndex && weightsByChildIndex[ci]) || 0);
        b.weights[i] = w;
        wSum += w;
      }
      if (wSum <= 1e-6) { b.weights[0] = 1; wSum = 1; }
      let newDur = 0;
      for (let i = 0; i < b.clips.length; i++) {
        b.weights[i] /= wSum;
        newDur += b.weights[i] * (b.clips[i].blendDuration || b.clips[i].duration || 1.0);
      }
      b.duration = Math.max(0.05, newDur);
      this._animState.time = phase * b.duration;
      if (this._skel) this._evaluatePose();
      if (this._onAnimTick) this._onAnimTick(this._animState, b);
      this.draw();
    }
    _activePlayback() {
      return this._blend || this._anim;
    }
    setAnimState(patch) {
      Object.assign(this._animState, patch || {});
      const act = this._activePlayback();
      if (act && act.duration > 0) {
        this._animState.time = Math.max(0, Math.min(act.duration, this._animState.time || 0));
      } else {
        this._animState.time = 0;
      }
      if (patch && patch.playing) this.lastTick = performance.now();
      if (this._skel) this._evaluatePose();
      if (this._onAnimTick) this._onAnimTick(this._animState, act);
      this.draw();
    }
    resetBindPose() {
      this._blend = null;
      this.setAnimation(null);
    }
    onAnimTick(fn) {
      this._onAnimTick = fn;
    }
    _mapBlendPhaseToChildTime(clip, phase) {
      const curve = clip.timeCurve;
      if (!curve || curve.length < 2) {
        return phase * (clip.duration || 0);
      }
      const bDur = clip.blendDuration || curve[curve.length - 1][1] || 1.0;
      const bt = phase * bDur;
      if (bt <= curve[0][1]) return curve[0][0];
      const last = curve[curve.length - 1];
      if (bt >= last[1]) return last[0];
      for (let k = 0; k < curve.length - 1; k++) {
        const b0 = curve[k][1], b1 = curve[k + 1][1];
        if (bt >= b0 && bt <= b1) {
          const span = b1 - b0;
          const u = span > 1e-6 ? (bt - b0) / span : 0;
          return curve[k][0] + (curve[k + 1][0] - curve[k][0]) * u;
        }
      }
      return phase * (clip.duration || 0);
    }
    _evaluatePose() {
      const skel = this._skel;
      if (!skel) return;
      const joints = skel.joints;
      const n = joints.length;
      if (!n) return;
      if (!this._worldScratch || this._worldScratch.length < n * 16) {
        this._worldScratch = new Float32Array(n * 16);
      }
      const worlds = this._worldScratch;
      const loc = this._locScratch || (this._locScratch = new Float32Array(16));
      const tmp = this._skinScratch || (this._skinScratch = new Float32Array(16));
      const anim = this._anim;
      const blend = this._blend;
      const hasActive = !!(anim || (blend && blend.clips.length));
      let f0 = 0, f1 = 0, alpha = 0;
      let mvx = 0, mvy = 0, mvz = 0, mrx = 0, mry = 0, mrz = 0, mrw = 1, hasMoverRot = false;
      let activeClips = null;

      if (blend && blend.clips.length) {
        const phase = blend.duration > 0 ? Math.max(0, Math.min(1, this._animState.time / blend.duration)) : 0;
        activeClips = [];
        let mvw = 0, mrRef = false;
        let bmx = 0, bmy = 0, bmz = 0, brx = 0, bry = 0, brz = 0, brw = 0;
        for (let ci = 0; ci < blend.clips.length; ci++) {
          const w = blend.weights[ci];
          if (w <= 1e-4) continue;
          const c = blend.clips[ci];
          const ct = this._mapBlendPhaseToChildTime(c, phase);
          const ff = c.duration > 0 ? (ct / c.duration) * Math.max(0, c.frames - 1) : 0;
          const cf0 = Math.max(0, Math.min(c.frames - 1, Math.floor(ff)));
          const cf1 = Math.min(c.frames - 1, cf0 + 1);
          const ca = Math.max(0, Math.min(1, ff - cf0));
          activeClips.push({ c, w, f0: cf0, f1: cf1, alpha: ca });
          if (this._animState.rootMotion) {
            if (c.moverPos) {
              const p0 = cf0 * 3, p1 = cf1 * 3;
              bmx += w * (c.moverPos[p0] + (c.moverPos[p1] - c.moverPos[p0]) * ca);
              bmy += w * (c.moverPos[p0 + 1] + (c.moverPos[p1 + 1] - c.moverPos[p0 + 1]) * ca);
              bmz += w * (c.moverPos[p0 + 2] + (c.moverPos[p1 + 2] - c.moverPos[p0 + 2]) * ca);
              mvw += w;
            }
            if (c.moverRot) {
              const r0 = cf0 * 4, r1 = cf1 * 4;
              let ax = c.moverRot[r0], ay = c.moverRot[r0 + 1], az = c.moverRot[r0 + 2], aw = c.moverRot[r0 + 3];
              let bx = c.moverRot[r1], by = c.moverRot[r1 + 1], bz = c.moverRot[r1 + 2], bw = c.moverRot[r1 + 3];
              if (ax * bx + ay * by + az * bz + aw * bw < 0) { bx = -bx; by = -by; bz = -bz; bw = -bw; }
              let rx = ax + (bx - ax) * ca, ry = ay + (by - ay) * ca, rz = az + (bz - az) * ca, rw = aw + (bw - aw) * ca;
              const rl = Math.hypot(rx, ry, rz, rw) || 1;
              rx /= rl; ry /= rl; rz /= rl; rw /= rl;
              if (mrRef && (rx * brx + ry * bry + rz * brz + rw * brw < 0)) { rx = -rx; ry = -ry; rz = -rz; rw = -rw; }
              brx += w * rx; bry += w * ry; brz += w * rz; brw += w * rw;
              mrRef = true;
            }
          }
        }
        if (this._animState.rootMotion) {
          if (mvw > 1e-4) { mvx = bmx / mvw; mvy = bmy / mvw; mvz = bmz / mvw; }
          if (mrRef) {
            const ml = Math.hypot(brx, bry, brz, brw) || 1;
            mrx = brx / ml; mry = bry / ml; mrz = brz / ml; mrw = brw / ml;
            hasMoverRot = true;
          }
        }
      } else if (anim && anim.frames > 0) {
        const ff = anim.duration > 0 ? (this._animState.time / anim.duration) * Math.max(0, anim.frames - 1) : 0;
        f0 = Math.max(0, Math.min(anim.frames - 1, Math.floor(ff)));
        f1 = Math.min(anim.frames - 1, f0 + 1);
        alpha = ff - f0;
        if (this._animState.rootMotion) {
          if (anim.moverPos) {
            const p0 = f0 * 3, p1 = f1 * 3;
            mvx = anim.moverPos[p0] + (anim.moverPos[p1] - anim.moverPos[p0]) * alpha;
            mvy = anim.moverPos[p0 + 1] + (anim.moverPos[p1 + 1] - anim.moverPos[p0 + 1]) * alpha;
            mvz = anim.moverPos[p0 + 2] + (anim.moverPos[p1 + 2] - anim.moverPos[p0 + 2]) * alpha;
          }
          if (anim.moverRot) {
            const r0 = f0 * 4, r1 = f1 * 4;
            let ax = anim.moverRot[r0], ay = anim.moverRot[r0 + 1], az = anim.moverRot[r0 + 2], aw = anim.moverRot[r0 + 3];
            let bx = anim.moverRot[r1], by = anim.moverRot[r1 + 1], bz = anim.moverRot[r1 + 2], bw = anim.moverRot[r1 + 3];
            if (ax * bx + ay * by + az * bz + aw * bw < 0) { bx = -bx; by = -by; bz = -bz; bw = -bw; }
            mrx = ax + (bx - ax) * alpha; mry = ay + (by - ay) * alpha;
            mrz = az + (bz - az) * alpha; mrw = aw + (bw - aw) * alpha;
            const ml = Math.hypot(mrx, mry, mrz, mrw) || 1;
            mrx /= ml; mry /= ml; mrz /= ml; mrw /= ml;
            hasMoverRot = true;
          }
        }
      }
      const boneData = this._boneData;
      for (let i = 0; i < n; i++) {
        const j = joints[i];
        let tx = j.trans[0], ty = j.trans[1], tz = j.trans[2];
        let qx = j.rot[0], qy = j.rot[1], qz = j.rot[2], qw = j.rot[3];
        let sx = j.scale[0], sy = j.scale[1], sz = j.scale[2];
        if (activeClips && activeClips.length) {
          let rAccX = 0, rAccY = 0, rAccZ = 0, rAccW = 0, rRef = false, rHas = false;
          let tAccX = 0, tAccY = 0, tAccZ = 0, tHas = false;
          let sAccX = 0, sAccY = 0, sAccZ = 0, sHas = false;
          for (let k = 0; k < activeClips.length; k++) {
            const ac = activeClips[k], c = ac.c, w = ac.w;
            const rt = c.rot.get(i);
            let rx, ry, rz, rw;
            if (rt) {
              rHas = true;
              const a = ac.f0 * 4, b = ac.f1 * 4;
              let ax = rt[a], ay = rt[a + 1], az = rt[a + 2], aw = rt[a + 3];
              let bx = rt[b], by = rt[b + 1], bz = rt[b + 2], bw = rt[b + 3];
              if (ax * bx + ay * by + az * bz + aw * bw < 0) { bx = -bx; by = -by; bz = -bz; bw = -bw; }
              rx = ax + (bx - ax) * ac.alpha; ry = ay + (by - ay) * ac.alpha;
              rz = az + (bz - az) * ac.alpha; rw = aw + (bw - aw) * ac.alpha;
              const rl = Math.hypot(rx, ry, rz, rw) || 1;
              rx /= rl; ry /= rl; rz /= rl; rw /= rl;
            } else {
              rx = qx; ry = qy; rz = qz; rw = qw;
            }
            if (rRef && (rx * rAccX + ry * rAccY + rz * rAccZ + rw * rAccW < 0)) {
              rx = -rx; ry = -ry; rz = -rz; rw = -rw;
            }
            rAccX += w * rx; rAccY += w * ry; rAccZ += w * rz; rAccW += w * rw;
            rRef = true;

            const tt = c.trans.get(i);
            if (tt) {
              tHas = true;
              const a = ac.f0 * 3, b = ac.f1 * 3;
              tAccX += w * (tt[a] + (tt[b] - tt[a]) * ac.alpha);
              tAccY += w * (tt[a + 1] + (tt[b + 1] - tt[a + 1]) * ac.alpha);
              tAccZ += w * (tt[a + 2] + (tt[b + 2] - tt[a + 2]) * ac.alpha);
            } else {
              tAccX += w * tx; tAccY += w * ty; tAccZ += w * tz;
            }
            const st = c.scale.get(i);
            if (st) {
              sHas = true;
              const a = ac.f0 * 3, b = ac.f1 * 3;
              sAccX += w * (st[a] + (st[b] - st[a]) * ac.alpha);
              sAccY += w * (st[a + 1] + (st[b + 1] - st[a + 1]) * ac.alpha);
              sAccZ += w * (st[a + 2] + (st[b + 2] - st[a + 2]) * ac.alpha);
            } else {
              sAccX += w * sx; sAccY += w * sy; sAccZ += w * sz;
            }
          }
          if (rHas) { qx = rAccX; qy = rAccY; qz = rAccZ; qw = rAccW; }
          if (tHas) { tx = tAccX; ty = tAccY; tz = tAccZ; }
          if (sHas) { sx = sAccX; sy = sAccY; sz = sAccZ; }
        } else if (anim) {
          const rt = anim.rot.get(i);
          if (rt) {
            const a = f0 * 4, b = f1 * 4;
            let ax = rt[a], ay = rt[a + 1], az = rt[a + 2], aw = rt[a + 3];
            let bx = rt[b], by = rt[b + 1], bz = rt[b + 2], bw = rt[b + 3];
            if (ax * bx + ay * by + az * bz + aw * bw < 0) { bx = -bx; by = -by; bz = -bz; bw = -bw; }
            qx = ax + (bx - ax) * alpha; qy = ay + (by - ay) * alpha;
            qz = az + (bz - az) * alpha; qw = aw + (bw - aw) * alpha;
          }
          const tt = anim.trans.get(i);
          if (tt) {
            const a = f0 * 3, b = f1 * 3;
            tx = tt[a] + (tt[b] - tt[a]) * alpha;
            ty = tt[a + 1] + (tt[b + 1] - tt[a + 1]) * alpha;
            tz = tt[a + 2] + (tt[b + 2] - tt[a + 2]) * alpha;
          }
          const st = anim.scale.get(i);
          if (st) {
            const a = f0 * 3, b = f1 * 3;
            sx = st[a] + (st[b] - st[a]) * alpha;
            sy = st[a + 1] + (st[b + 1] - st[a + 1]) * alpha;
            sz = st[a + 2] + (st[b + 2] - st[a + 2]) * alpha;
          }
        }
        const ql = Math.hypot(qx, qy, qz, qw) || 1;
        qx /= ql; qy /= ql; qz /= ql; qw /= ql;
        if (j.parent < 0 && this._animState.rootMotion && hasActive) {
          if (hasMoverRot) {
            const nx = mrw * qx + mrx * qw + mry * qz - mrz * qy;
            const ny = mrw * qy - mrx * qz + mry * qw + mrz * qx;
            const nz = mrw * qz + mrx * qy - mry * qx + mrz * qw;
            const nw = mrw * qw - mrx * qx - mry * qy - mrz * qz;
            qx = nx; qy = ny; qz = nz; qw = nw;
          }
          tx += mvx; ty += mvy; tz += mvz;
        }
        // Apply RAGE 9 Dynamic Physiology (27-channel BM_* bone morphs + height + SEC_* soft-tissue physics) & Dismemberment
        if (this._hiddenBoneIdx && this._hiddenBoneIdx.has(i)) {
          sx = 0.0001; sy = 0.0001; sz = 0.0001;
        } else {
          const cm = this.charMorph;
          const bn = j.name;
          if (j.parent < 0 && cm && cm.defaultHeight > 0.5 && Math.abs(cm.height - cm.defaultHeight) > 0.002) {
            const hs = Math.max(0.78, Math.min(1.26, cm.height / cm.defaultHeight));
            sx *= hs; sy *= hs; sz *= hs;
          }
          if (cm && (j.group === "BM" || j.group === "SEC")) {
            const m = this._evalBoneMorph(bn, cm);
            if (m) {
              sx *= m[0]; sy *= m[1]; sz *= m[2];
              tx += m[3]; ty += m[4]; tz += m[5];
            }
          }
        }
        // Compose column-major TRS matrix in RAGE Z-up space.
        const xx = qx * qx, yy = qy * qy, zz = qz * qz;
        const xy = qx * qy, xz = qx * qz, yz = qy * qz;
        const wx = qw * qx, wy = qw * qy, wz = qw * qz;
        loc[0]  = (1 - 2 * (yy + zz)) * sx;
        loc[1]  = (2 * (xy + wz)) * sx;
        loc[2]  = (2 * (xz - wy)) * sx;
        loc[3]  = 0;
        loc[4]  = (2 * (xy - wz)) * sy;
        loc[5]  = (1 - 2 * (xx + zz)) * sy;
        loc[6]  = (2 * (yz + wx)) * sy;
        loc[7]  = 0;
        loc[8]  = (2 * (xz + wy)) * sz;
        loc[9]  = (2 * (yz - wx)) * sz;
        loc[10] = (1 - 2 * (xx + yy)) * sz;
        loc[11] = 0;
        loc[12] = tx; loc[13] = ty; loc[14] = tz; loc[15] = 1;

        const wo = i * 16;
        const par = j.parent;
        if (par >= 0 && par < i) {
          const po = par * 16;
          for (let c = 0; c < 4; c++) {
            const b0 = loc[c * 4], b1 = loc[c * 4 + 1], b2 = loc[c * 4 + 2], b3 = loc[c * 4 + 3];
            worlds[wo + c * 4]     = worlds[po] * b0 + worlds[po + 4] * b1 + worlds[po + 8] * b2 + worlds[po + 12] * b3;
            worlds[wo + c * 4 + 1] = worlds[po + 1] * b0 + worlds[po + 5] * b1 + worlds[po + 9] * b2 + worlds[po + 13] * b3;
            worlds[wo + c * 4 + 2] = worlds[po + 2] * b0 + worlds[po + 6] * b1 + worlds[po + 10] * b2 + worlds[po + 14] * b3;
            worlds[wo + c * 4 + 3] = worlds[po + 3] * b0 + worlds[po + 7] * b1 + worlds[po + 11] * b2 + worlds[po + 15] * b3;
          }
        } else {
          for (let k = 0; k < 16; k++) worlds[wo + k] = loc[k];
        }

        // Skin matrix in RAGE Z-up: M = world * ibm
        const ibm = j._ibm;
        for (let c = 0; c < 4; c++) {
          const b0 = ibm[c * 4], b1 = ibm[c * 4 + 1], b2 = ibm[c * 4 + 2], b3 = ibm[c * 4 + 3];
          tmp[c * 4]     = worlds[wo] * b0 + worlds[wo + 4] * b1 + worlds[wo + 8] * b2 + worlds[wo + 12] * b3;
          tmp[c * 4 + 1] = worlds[wo + 1] * b0 + worlds[wo + 5] * b1 + worlds[wo + 9] * b2 + worlds[wo + 13] * b3;
          tmp[c * 4 + 2] = worlds[wo + 2] * b0 + worlds[wo + 6] * b1 + worlds[wo + 10] * b2 + worlds[wo + 14] * b3;
          tmp[c * 4 + 3] = worlds[wo + 3] * b0 + worlds[wo + 7] * b1 + worlds[wo + 11] * b2 + worlds[wo + 15] * b3;
        }
        // Convert M from RAGE Z-up to glTF Y-up via C * M * C^T directly into boneTex row i.
        boneData[wo]      = tmp[0];  boneData[wo + 1]  = tmp[2];  boneData[wo + 2]  = -tmp[1];  boneData[wo + 3]  = 0;
        boneData[wo + 4]  = tmp[8];  boneData[wo + 5]  = tmp[10]; boneData[wo + 6]  = -tmp[9];  boneData[wo + 7]  = 0;
        boneData[wo + 8]  = -tmp[4]; boneData[wo + 9]  = -tmp[6]; boneData[wo + 10] = tmp[5];   boneData[wo + 11] = 0;
        boneData[wo + 12] = tmp[12]; boneData[wo + 13] = tmp[14]; boneData[wo + 14] = -tmp[13]; boneData[wo + 15] = 1;
      }
      const gl = this.gl;
      gl.bindTexture(gl.TEXTURE_2D, this._boneTex);
      gl.texSubImage2D(gl.TEXTURE_2D, 0, 0, 0, 4, n, gl.RGBA, gl.FLOAT, boneData.subarray(0, n * 16));
      if (this.opts.skeleton || this._showVitalOrgans) this._rebuildSkelOverlay();
    }
    _evalBoneMorph(name, cm) {
      let sx = 1, sy = 1, sz = 1, dx = 0, dy = 0, dz = 0;
      const cl = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
      const wAll = cm.weight || 0, mAll = cm.muscle || 0;
      const wCh = (k) => cl((cm[k] || 0) + wAll, -1.0, 1.35);
      const mCh = (k) => cl((cm[k] || 0) + mAll, -0.35, 1.35);
      const jig = this._jiggle;
      const isMale = (cm.defaultHeight || 1.72) > 1.77;
      // Verified RAGE 9 skeleton local axes (character faces -Y_world, left is +X_world, up is +Z_world):
      // - Upper torso / breasts / stomach / spine (parent MH_RibMid / SKEL_Spine*): +X = UP, +Y = FORWARD, +Z = LEFT (+X_world)
      // - Pelvis / butt / thighs / calves (parent BM_pelvis / SKEL_Pelvis / RB_*_Thigh*): +X = DOWN, +Y = FORWARD, +Z = RIGHT (-X_world)
      // - Upper arm / bicep / tricep / forearm: +X = along limb, +Y & +Z = cross-section girth
      if (name === "BM_L_Breast" || name === "BM_R_Breast") {
        const bv = (cm["0xb0eae798"] || cm.gcbreastvolume || 0);
        const s = isMale
          ? ((cm.GCBreasts || 0) * 0.24 + wCh("gcweightchest") * 0.14 + mCh("gcmusclechest") * 0.12)
          : ((cm.GCBreasts || 0) * 0.55 + bv * 0.24 + wCh("gcweightchest") * 0.16);
        const c = (cm.GCCleavage || 0) * 0.016;
        const sp = (cm.gcbreastspacing || 0) * 0.018;
        const bh = (cm.gcbreastheight || 0) * 0.018 + (cm.gcchestheight || 0) * 0.012;
        const sgn = name.startsWith("BM_L_") ? 1 : -1;
        if (isMale) {
          sx += s * 0.25; sy += s * 0.42; sz += s * 0.36;
          dx += bh * 0.5;
          dy += s * 0.012;
        } else {
          sx += s * 0.52; sy += s * 0.85; sz += s * 0.55;
          dx += bh;
          dy += s * 0.024 + bv * 0.008;
          dz += sgn * (sp - c);
        }
      } else if (name === "SEC_L_Full_Breast" || name === "SEC_R_Full_Breast" || name === "SEC_L_Mid_Breast" || name === "SEC_R_Mid_Breast") {
        const bv = (cm["0xb0eae798"] || cm.gcbreastvolume || 0);
        const s = isMale ? 0 : ((cm.GCBreasts || 0) * 0.25 + bv * 0.18);
        const gain = name.includes("_Mid_") ? 1.35 : 0.85;
        sx += s * 0.28; sy += s * 0.45; sz += s * 0.28;
        if (jig && (cm.jiggleChest || 0) > 0.005) {
          const jg = (cm.jiggleChest || 0) * gain;
          dx += cl(jig.dispChest[2] * jg, -0.035, 0.035);
          dy += cl(-jig.dispChest[1] * jg, -0.028, 0.028);
          dz += cl(jig.dispChest[0] * jg, -0.028, 0.028);
        }
      } else if (name === "BM_L_Top_Breast" || name === "BM_R_Top_Breast" || name === "BM_L_Bot_Breast" || name === "BM_R_Bot_Breast") {
        const bv = (cm["0xb0eae798"] || cm.gcbreastvolume || 0);
        const isBot = name.includes("_Bot_");
        const s = isMale ? (cm.GCBreasts || 0) * 0.12 : ((cm.GCBreasts || 0) * 0.25 + bv * (isBot ? 0.32 : 0.14));
        sx += s * 0.20; sy += s * 0.42; sz += s * 0.25;
        if (!isMale && isBot) dy += bv * 0.010;
      } else if (name === "BM_Cleavage") {
        const c = (cm.GCCleavage || 0);
        dy += c * 0.014; sz -= c * 0.16;
      } else if (name.includes("Pectoralis")) {
        const mv = mCh("gcmusclechest") + (isMale ? Math.max(0, cm.GCBreasts || 0) * 0.45 : 0);
        const wv = wCh("gcweightchest");
        sx += mv * 0.14 + wv * 0.10; sy += mv * 0.28 + wv * 0.20; sz += mv * 0.18 + wv * 0.12;
        dy += mv * 0.012 + Math.max(0, wv) * 0.010;
      } else if (name === "BM_L_Butt" || name === "BM_R_Butt") {
        const b = (cm.GCButt || 0) * 0.55 + wCh("gcweightbutt") * 0.45 + mCh("gcmusclebutt") * 0.42;
        const hw = cm.gchipwidth || 0;
        const hh = cm.gchipheight || 0;
        const sgn = name.startsWith("BM_L_") ? 1 : -1;
        sx += b * 0.28;
        sy += b * 0.85;
        sz += b * 0.48 + hw * 0.24;
        dx -= mCh("gcmusclebutt") * 0.008 - Math.max(0, wCh("gcweightbutt")) * 0.004 + hh * 0.012;
        dy -= b * 0.052;
        dz -= sgn * (hw * 0.022 + b * 0.010);
        if (jig && (cm.jiggleButt || 0) > 0.005) {
          const jg = cm.jiggleButt || 0;
          dx += cl(-jig.dispButt[2] * jg, -0.032, 0.032);
          dy += cl(-jig.dispButt[1] * jg, -0.025, 0.025);
          dz += cl(-jig.dispButt[0] * jg, -0.025, 0.025);
        }
      } else if (name === "BM_L_Pelvis" || name === "BM_R_Pelvis") {
        const wv = wCh("gcweighthip"), mv = mCh("gcmusclehip");
        const hw = cm.gchipwidth || 0;
        const hh = cm.gchipheight || 0;
        const b = (cm.GCButt || 0) * 0.35 + wCh("gcweightbutt") * 0.20 + mCh("gcmusclebutt") * 0.15;
        const sgn = name.startsWith("BM_L_") ? 1 : -1;
        sy += wv * 0.22 + mv * 0.10 + b * 0.18;
        sz += hw * 0.34 + wv * 0.24 + mv * 0.12;
        dx -= hh * 0.014;
        dz -= sgn * (hw * 0.026 + wv * 0.010);
      } else if (name === "BM_pelvisRear" || name === "BM_pelvisRearLo") {
        const wv = wCh("gcweighthip"), hw = cm.gchipwidth || 0;
        const b = (cm.GCButt || 0) * 0.45 + wCh("gcweightbutt") * 0.32 + mCh("gcmusclebutt") * 0.28;
        sy += b * 0.36 + wv * 0.14;
        sz += hw * 0.22 + wv * 0.18 + b * 0.18;
        dy -= b * 0.018;
      } else if (name === "BM_pelvis" || name === "BM_pelvisFront") {
        const wv = wCh("gcweighthip"), mv = mCh("gcmusclehip"), hw = cm.gchipwidth || 0;
        sy += wv * 0.16 + (cm.GCButt || 0) * 0.08;
        sz += hw * 0.16 + wv * 0.16 + mv * 0.08;
      } else if (name.startsWith("BM_Stomach")) {
        const wb = wCh("gcweightbelly"), wm = wCh("gcweightmuffin"), mb = mCh("gcmusclebelly");
        const br = cm.gcbellyround || 0;
        const isSide = name.includes("_L_") || name.includes("_R_");
        const v = isSide ? (wm * 0.28 + wb * 0.16) : (wb * 0.34 + wm * 0.12);
        sx += v * 0.12;
        sy += v * 0.72 + br * 0.42 + mb * 0.10;
        sz += v * 0.52 + wm * 0.22 + mb * 0.08;
        dy += Math.max(0, v + br * 0.5) * 0.042;
        if (jig && (cm.jiggleBelly || 0) > 0.005) {
          const jg = cm.jiggleBelly || 0;
          dx += cl(jig.dispBelly[2] * jg, -0.028, 0.028);
          dy += cl(-jig.dispBelly[1] * jg, -0.024, 0.024);
        }
      } else if (name.includes("Spine0") || name.includes("Spine1")) {
        const wm = wCh("gcweightmuffin"), wb = wCh("gcweightback"), mb = mCh("gcmuscleback");
        const mm = mCh("gcmusclemuffin");
        sy += wm * 0.18 + wb * 0.14 + mb * 0.08;
        sz += wm * 0.22 + wb * 0.12 + mb * 0.08 + mm * 0.10;
      } else if (name.includes("Spine2") || name.includes("Spine3") || name.includes("Spine4") || name.includes("Spine5")) {
        const wb = wCh("gcweightback"), wc = wCh("gcweightchest"), mb = mCh("gcmuscleback");
        sy += wb * 0.14 + wc * 0.14 + mb * 0.16;
        sz += wb * 0.14 + wc * 0.12 + mb * 0.20;
      } else if (name.includes("Deltoid") || name.includes("Clavicle") || name.includes("Scapula") || name.includes("Trap")) {
        const ws = wCh("gcweightshoulder"), ms = mCh("gcmuscleshoulder"), mb = mCh("gcmuscleback");
        sx += ws * 0.08 + ms * 0.16;
        sy += ws * 0.16 + ms * 0.24;
        sz += ws * 0.16 + ms * 0.26 + (name.includes("Trap") ? mb * 0.16 : 0);
      } else if (name.includes("Bicep")) {
        const wu = wCh("gcweightupperarm"), mu = mCh("gcmuscleupperarm"), mb = mCh("gcmusclebiceps");
        sy += wu * 0.22 + mu * 0.26 + mb * 0.24;
        sz += wu * 0.22 + mu * 0.26 + mb * 0.24;
      } else if (name.includes("Tricep")) {
        const wu = wCh("gcweightupperarm"), mu = mCh("gcmuscleupperarm"), mt = mCh("gcmuscletriceps");
        sy += wu * 0.22 + mu * 0.26 + mt * 0.24;
        sz += wu * 0.22 + mu * 0.26 + mt * 0.24;
      } else if (name.includes("UpperArm")) {
        const wu = wCh("gcweightupperarm"), mu = mCh("gcmuscleupperarm");
        sy += wu * 0.20 + mu * 0.26;
        sz += wu * 0.20 + mu * 0.26;
      } else if (name.includes("Forearm") || name.includes("Brachioradialis")) {
        const wf = wCh("gcweightforearm"), mf = mCh("gcmuscleforearm");
        sy += wf * 0.18 + mf * 0.24;
        sz += wf * 0.18 + mf * 0.24;
      } else if (name.includes("Thigh") || name.includes("Quadriceps") || name.includes("Hamstrings")) {
        const wt = wCh("gcweightthigh"), mt = mCh("gcmusclethigh"), hw = cm.gchipwidth || 0;
        sy += wt * 0.30 + mt * 0.28;
        sz += wt * 0.30 + mt * 0.26 + hw * 0.14;
      } else if (name.includes("Calf")) {
        const wc = wCh("gcweightcalf"), mc = mCh("gcmusclecalf");
        sy += wc * 0.26 + mc * 0.28;
        sz += wc * 0.24 + mc * 0.26;
      } else if (name.includes("Neck") || name.includes("Throat") || name.includes("Mastoid")) {
        const wn = wCh("gcweightneck"), mn = mCh("gcmuscleneck");
        sy += wn * 0.16 + mn * 0.20;
        sz += wn * 0.16 + mn * 0.22;
      } else if (name.includes("Cheek") || name === "BM_Chin") {
        const wn = wCh("gcweightneck");
        sx += wn * 0.10; sy += wn * 0.16; sz += wn * 0.14;
      } else {
        return null;
      }
      if (Math.abs(sx - 1) < 1e-4 && Math.abs(sy - 1) < 1e-4 && Math.abs(sz - 1) < 1e-4 &&
          Math.abs(dx) < 1e-5 && Math.abs(dy) < 1e-5 && Math.abs(dz) < 1e-5) return null;
      return [cl(sx, 0.55, 1.85), cl(sy, 0.55, 1.85), cl(sz, 0.55, 1.85), dx, dy, dz];
    }
    _stepJiggle(dt) {
      const cm = this.charMorph;
      if (!this._skel || !this._worldScratch || !cm) return false;
      const anyOn = (cm.jiggleChest || 0) > 0.005 || (cm.jiggleButt || 0) > 0.005 || (cm.jiggleBelly || 0) > 0.005;
      if (!anyOn) return false;
      const jg = this._jiggle;
      const joints = this._skel.joints;
      const findPos = (nm) => {
        for (let i = 0; i < joints.length; i++) {
          if (joints[i].name === nm) {
            const wo = i * 16;
            return [this._worldScratch[wo + 12], this._worldScratch[wo + 13], this._worldScratch[wo + 14]];
          }
        }
        return null;
      };
      const k = Math.max(8, cm.jiggleStiff || 30);
      const c = Math.max(1.2, cm.jiggleDamp || 4.2);
      const stepOsc = (disp, vel, curPos, prevPos, prevVel) => {
        let ax = 0, ay = 0, az = 0;
        if (curPos && prevPos && dt > 1e-4) {
          const vx = (curPos[0] - prevPos[0]) / dt;
          const vy = (curPos[1] - prevPos[1]) / dt;
          const vz = (curPos[2] - prevPos[2]) / dt;
          ax = Math.max(-18, Math.min(18, -(vx - prevVel[0]) / dt));
          ay = Math.max(-18, Math.min(18, -(vy - prevVel[1]) / dt));
          az = Math.max(-18, Math.min(18, -(vz - prevVel[2]) / dt));
          prevVel[0] = vx; prevVel[1] = vy; prevVel[2] = vz;
        }
        const acc = [ax * 0.022, ay * 0.022, az * 0.026];
        for (let a = 0; a < 3; a++) {
          const f = -k * disp[a] - c * vel[a] + acc[a];
          vel[a] += f * dt;
          disp[a] += vel[a] * dt;
          disp[a] = Math.max(-0.045, Math.min(0.045, disp[a]));
        }
        return curPos ? [curPos[0], curPos[1], curPos[2]] : prevPos;
      };
      const chPos = findPos("SKEL_Spine3") || findPos("SKEL_Spine2");
      const pvPos = findPos("SKEL_Pelvis") || findPos("SKEL_ROOT");
      jg.prevChest = stepOsc(jg.dispChest, jg.velChest, chPos, jg.prevChest, jg.prevVelChest);
      jg.prevPelvis = stepOsc(jg.dispButt, jg.velButt, pvPos, jg.prevPelvis, jg.prevVelPelvis);
      stepOsc(jg.dispBelly, jg.velBelly, pvPos, jg.prevPelvis, jg.prevVelPelvis);
      const energy = Math.hypot(...jg.dispChest, ...jg.velChest, ...jg.dispButt, ...jg.velButt, ...jg.dispBelly, ...jg.velBelly);
      return energy > 0.0008;
    }
    _rebuildHiddenBoneSet() {
      this._hiddenBoneIdx = new Set();
      if (!this._skel || !this._hiddenBoneRoots.size) return;
      const joints = this._skel.joints;
      for (let i = 0; i < joints.length; i++) {
        if (this._hiddenBoneRoots.has(joints[i].name)) this._hiddenBoneIdx.add(i);
      }
      // Propagate to all descendants in topological order
      let changed = true;
      while (changed) {
        changed = false;
        for (let i = 0; i < joints.length; i++) {
          if (!this._hiddenBoneIdx.has(i) && joints[i].parent >= 0 && this._hiddenBoneIdx.has(joints[i].parent)) {
            this._hiddenBoneIdx.add(i);
            changed = true;
          }
        }
      }
    }
    setHiddenBoneRoots(names) {
      this._hiddenBoneRoots = new Set(names || []);
      this._rebuildHiddenBoneSet();
      if (this._skel) this._evaluatePose();
      this.draw();
    }
    setCharFx(patch) {
      Object.assign(this.charFx, patch || {});
      this.draw();
    }
    setWounds(list) {
      this.charFx.wounds = (list || []).slice(0, 8);
      this.draw();
    }
    setBloodDecalTexture(img, tintable = false) {
      if (this._bloodDecalTex) { this.gl.deleteTexture(this._bloodDecalTex); this._bloodDecalTex = null; }
      this._bloodDecalTintable = !!tintable;
      if (img) this._bloodDecalTex = this._uploadTexture(img, false);
      this.draw();
    }
    setCharMorph(patch) {
      Object.assign(this.charMorph, patch || {});
      if (this._skel) this._evaluatePose();
      this.draw();
    }
    resetCharMorph() {
      const defH = this.charMorph.defaultHeight || 1.72;
      for (const k of Object.keys(this.charMorph)) {
        if (k === "defaultHeight" || k === "height") this.charMorph[k] = defH;
        else if (k === "jiggleStiff") this.charMorph[k] = 30.0;
        else if (k === "jiggleDamp") this.charMorph[k] = 4.2;
        else this.charMorph[k] = 0.0;
      }
      if (this._skel) this._evaluatePose();
      this.draw();
    }
    impulseJiggle(strength = 1.0) {
      const s = Math.max(0.2, Math.min(3.0, strength));
      const jg = this._jiggle;
      jg.velChest[2] -= 0.65 * s; jg.velChest[1] -= 0.25 * s;
      jg.velButt[2] -= 0.55 * s; jg.velButt[1] += 0.28 * s;
      jg.velBelly[2] -= 0.45 * s; jg.velBelly[1] -= 0.22 * s;
      this.lastTick = performance.now();
      this.draw();
    }
    setVitalOrgans(organs, show) {
      this._vitalOrgans = organs || [];
      this._showVitalOrgans = !!show;
      if (this._skel) this._rebuildSkelOverlay();
      this.draw();
    }
    setPostFx(patch) {
      Object.assign(this.postFx, patch || {});
      this._samplePostFxCurve();
      this.lastTick = performance.now();
      this.draw();
    }
    onPostFxTick(fn) { this._onPostFxTick = fn; }
    setPickMode(on, cb = null) {
      this._pickMode = !!on;
      this._onSurfacePick = cb;
      this.canvas.style.cursor = this._pickMode ? "crosshair" : "";
    }
    _sampleCurve(times, vals, u) {
      if (!vals || !vals.length) return null;
      if (vals.length === 1 || !times || times.length <= 1) return vals[0];
      const n = Math.min(times.length, vals.length);
      if (u <= times[0]) return vals[0];
      if (u >= times[n - 1]) return vals[n - 1];
      for (let i = 0; i < n - 1; i++) {
        if (u >= times[i] && u <= times[i + 1]) {
          const span = times[i + 1] - times[i];
          const a = span > 1e-6 ? (u - times[i]) / span : 0;
          return vals[i] + (vals[i + 1] - vals[i]) * a;
        }
      }
      return vals[n - 1];
    }
    _samplePostFxCurve() {
      const pf = this.postFx;
      const pre = pf.preset;
      if (!pf.active || !pre) {
        this.charFx.glints = 0; this.charFx.pedLightMult = 1.0; this.charFx.pedFresnel = 0; this.charFx.uvParty = 0;
        return;
      }
      const u = ((pf.phase % 1) + 1) % 1;
      const t = pre.times || [0], pm = pre.params || {};
      const g = (k, def) => {
        const v = this._sampleCurve(t, pm[k], u);
        return v == null ? def : v;
      };
      pf.bloom = g("postfx_intensity_bloom", 0);
      pf.lensK1 = g("lens_dist_coeff", 0);
      pf.lensK2 = g("lens_dist_cube_coeff", 0);
      pf.chromK1 = g("chrom_aberration_coeff", 0);
      pf.chromK2 = g("chrom_aberration_coeff2", 0);
      pf.chromCol = [g("chrom_aberration_split_col_r", 0), g("chrom_aberration_split_col_g", 0), g("chrom_aberration_split_col_b", 0)];
      pf.vigInt = g("postfx_vignette_intensity", 0);
      pf.vigIn = g("postfx_vignette_inner_radius", 0.5);
      pf.vigOut = g("postfx_vignette_outer_radius", 2.5);
      pf.vigCol = [
        g("postfx_vignette_col_r", g("postfx_vignette_tint_r", 0)),
        g("postfx_vignette_col_g", g("postfx_vignette_tint_g", 0)),
        g("postfx_vignette_col_b", g("postfx_vignette_tint_b", 0)),
      ];
      pf.vigBloom = g("postfx_vignette_bloom_intensity", 0);
      pf.desat = g("postfx_desaturation", 1.0);
      pf.shCon = g("postfx_local_exposure_sh_contrast", 1.0);
      pf.hlCon = g("postfx_local_exposure_hl_contrast", 1.0);
      pf.dtCon = g("postfx_local_exposure_dt_contrast", 1.0);
      pf.noise = g("postfx_noise", 0);
      pf.exposure = g("postfx_exposure_tweak", g("postfx_exposure", 0));
      pf.trail = g("postfx_screen_trails_intensity", 0);
      pf.redShadow = Math.max(0, -g("postfx_color_grading_shadows_gamma_r", 0) * 0.35);
      pf.uvParty = g("uv_party_mode", 0);
      // Character-level shader hooks from drug modifiers (Mandy_Spangled glints, Molly ped rim light, Coke UV)
      const glintDens = g("brdf_glints_global_microfacet_density", 0);
      const glintRand = g("brdf_glints_global_density_randomization", 0);
      const pedMultRaw = g("ped_light0_mult", 1.0);
      const pedFresRaw = g("ped_light_fresnel", 0.0);
      const inten = pf.intensity || 0;
      this.charFx.glints = Math.min(1.0, glintDens * (0.35 + 0.65 * glintRand)) * inten;
      this.charFx.pedLightMult = 1.0 + Math.min(2.6, Math.log2(Math.max(1.0, pedMultRaw)) * 0.45) * inten;
      this.charFx.pedFresnel = Math.min(1.5, pedFresRaw) * inten;
      this.charFx.uvParty = pf.uvParty * inten;
      pf.liveReadout = {
        phase: u, bloom: pf.bloom, lensK2: pf.lensK2, chrom: Math.abs(pf.chromK1) + Math.abs(pf.chromK2),
        vig: pf.vigInt, desat: pf.desat, glints: this.charFx.glints,
      };
    }
    _ensurePostFbo(w, h) {
      if (this._postFbo && this._postW === w && this._postH === h) return true;
      const gl = this.gl;
      if (this._postFbo) {
        gl.deleteFramebuffer(this._postFbo); gl.deleteTexture(this._postTex); gl.deleteRenderbuffer(this._postDepth);
        gl.deleteFramebuffer(this._trailFbo); gl.deleteTexture(this._trailTex);
      }
      const mkTex = () => {
        const t = gl.createTexture();
        gl.bindTexture(gl.TEXTURE_2D, t);
        gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA8, w, h, 0, gl.RGBA, gl.UNSIGNED_BYTE, null);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
        return t;
      };
      this._postTex = mkTex();
      this._postDepth = gl.createRenderbuffer();
      gl.bindRenderbuffer(gl.RENDERBUFFER, this._postDepth);
      gl.renderbufferStorage(gl.RENDERBUFFER, gl.DEPTH_COMPONENT24, w, h);
      this._postFbo = gl.createFramebuffer();
      gl.bindFramebuffer(gl.FRAMEBUFFER, this._postFbo);
      gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, this._postTex, 0);
      gl.framebufferRenderbuffer(gl.FRAMEBUFFER, gl.DEPTH_ATTACHMENT, gl.RENDERBUFFER, this._postDepth);
      this._trailTex = mkTex();
      this._trailFbo = gl.createFramebuffer();
      gl.bindFramebuffer(gl.FRAMEBUFFER, this._trailFbo);
      gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, this._trailTex, 0);
      gl.clearColor(0, 0, 0, 1); gl.clear(gl.COLOR_BUFFER_BIT);
      gl.bindFramebuffer(gl.FRAMEBUFFER, null);
      this._postW = w; this._postH = h;
      return true;
    }
    _hasActiveCharFx() {
      const f = this.charFx;
      return f.wetness > 0.005 || f.soak > 0.005 || f.dust > 0.005 || f.dirtAmt > 0.005 ||
        f.underwater > 0.005 || Math.abs(f.sunTan) > 0.005 || f.sunBurn > 0.005 || f.flush > 0.005 ||
        f.bloodSoak > 0.005 || f.glints > 0.005 || Math.abs(f.pedLightMult - 1.0) > 0.01 ||
        f.pedFresnel > 0.005 || f.uvParty > 0.005 || f.eyeWetness > 0.25 || (f.wounds && f.wounds.length > 0);
    }
    pickSurface(clientX, clientY) {
      const rect = this.canvas.getBoundingClientRect();
      if (!rect.width || !rect.height) return null;
      const nx = ((clientX - rect.left) / rect.width) * 2 - 1;
      const ny = 1 - ((clientY - rect.top) / rect.height) * 2;
      const eye = this.eye();
      const fwd = norm(sub(this.cam.target, eye));
      const right = norm(cross(fwd, [0, 1, 0]));
      const up = cross(right, fwd);
      const t = Math.tan(0.75 * 0.5);
      const asp = rect.width / rect.height;
      const dir = norm([
        fwd[0] + right[0] * nx * t * asp + up[0] * ny * t,
        fwd[1] + right[1] * nx * t * asp + up[1] * ny * t,
        fwd[2] + right[2] * nx * t * asp + up[2] * ny * t,
      ]);
      let bestT = Infinity, bestP = null;
      for (const p of this.prims) {
        if (!p._rawPos || !p._rawIdx) continue;
        const m = this.materials.get(p.layer + ":" + p.shader);
        if (m && !m.visible) continue;
        const pos = p._rawPos, idx = p._rawIdx, M = p.model;
        for (let k = 0; k < idx.length; k += 3) {
          const ia = idx[k] * 3, ib = idx[k + 1] * 3, ic = idx[k + 2] * 3;
          const ax = M[0]*pos[ia] + M[4]*pos[ia+1] + M[8]*pos[ia+2] + M[12];
          const ay = M[1]*pos[ia] + M[5]*pos[ia+1] + M[9]*pos[ia+2] + M[13];
          const az = M[2]*pos[ia] + M[6]*pos[ia+1] + M[10]*pos[ia+2] + M[14];
          const bx = M[0]*pos[ib] + M[4]*pos[ib+1] + M[8]*pos[ib+2] + M[12];
          const by = M[1]*pos[ib] + M[5]*pos[ib+1] + M[9]*pos[ib+2] + M[13];
          const bz = M[2]*pos[ib] + M[6]*pos[ib+1] + M[10]*pos[ib+2] + M[14];
          const cx = M[0]*pos[ic] + M[4]*pos[ic+1] + M[8]*pos[ic+2] + M[12];
          const cy = M[1]*pos[ic] + M[5]*pos[ic+1] + M[9]*pos[ic+2] + M[13];
          const cz = M[2]*pos[ic] + M[6]*pos[ic+1] + M[10]*pos[ic+2] + M[14];
          const e1x = bx - ax, e1y = by - ay, e1z = bz - az;
          const e2x = cx - ax, e2y = cy - ay, e2z = cz - az;
          const px = dir[1]*e2z - dir[2]*e2y, py = dir[2]*e2x - dir[0]*e2z, pz = dir[0]*e2y - dir[1]*e2x;
          const det = e1x*px + e1y*py + e1z*pz;
          if (Math.abs(det) < 1e-8) continue;
          const invD = 1 / det;
          const tx = eye[0] - ax, ty = eye[1] - ay, tz = eye[2] - az;
          const u = (tx*px + ty*py + tz*pz) * invD;
          if (u < 0 || u > 1) continue;
          const qx = ty*e1z - tz*e1y, qy = tz*e1x - tx*e1z, qz = tx*e1y - ty*e1x;
          const v = (dir[0]*qx + dir[1]*qy + dir[2]*qz) * invD;
          if (v < 0 || u + v > 1) continue;
          const hitT = (e2x*qx + e2y*qy + e2z*qz) * invD;
          if (hitT > 0.01 && hitT < bestT) {
            bestT = hitT;
            bestP = [eye[0] + dir[0]*hitT, eye[1] + dir[1]*hitT, eye[2] + dir[2]*hitT];
          }
        }
      }
      return bestP;
    }
    _rebuildSkelOverlay() {
      const skel = this._skel;
      if (!skel || !this.skelProg || !this._worldScratch) {
        this._skelLineCount = 0;
        this._skelPtCount = 0;
        return;
      }
      const joints = skel.joints, n = joints.length, worlds = this._worldScratch;
      const filt = this.opts.skelFilter || "core";
      const sel = this.opts.selectedBone;
      const GROUP_COL = {
        SKEL: [0.22, 0.90, 0.94],
        FB:   [0.96, 0.44, 0.60],
        BM:   [0.95, 0.72, 0.28],
        MH:   [0.70, 0.50, 0.96],
        PH:   [0.36, 0.86, 0.56],
        GM:   [1.00, 0.58, 0.24],
        RB:   [0.54, 0.76, 0.95],
        SEC:  [0.64, 0.80, 0.45],
        OTHER:[0.56, 0.66, 0.72],
      };
      const showJoint = (j, idx) => {
        if (idx === sel) return true;
        if (filt === "all") return true;
        const g = j.group || "OTHER";
        if (filt === "face") return g === "SKEL" || g === "FB" || g === "GM";
        return g === "SKEL" || g === "GM";
      };
      const lines = [], pts = [];
      if (this.opts.skeleton) {
        for (let i = 0; i < n; i++) {
          const j = joints[i];
          if (!showJoint(j, i)) continue;
          const wo = i * 16;
          const cx = worlds[wo + 12], cy = worlds[wo + 14], cz = -worlds[wo + 13];
          const isSel = i === sel;
          const col = isSel ? [1.0, 0.93, 0.28] : (GROUP_COL[j.group] || GROUP_COL.OTHER);
          const psize = isSel ? 11.0 : (j.group === "SKEL" ? 6.0 : 4.5);
          pts.push(cx, cy, cz, col[0], col[1], col[2], psize);
          const par = j.parent;
          if (par >= 0 && par < n) {
            const po = par * 16;
            const px = worlds[po + 12], py = worlds[po + 14], pz = -worlds[po + 13];
            const pcol = isSel ? col : [col[0] * 0.75, col[1] * 0.75, col[2] * 0.75];
            lines.push(px, py, pz, pcol[0], pcol[1], pcol[2], 1.0);
            lines.push(cx, cy, cz, col[0], col[1], col[2], 1.0);
          }
        }
      }
      // Vital organ hitboxes from 000000046000.xml (Heart, Throat, Brain, Spine) attached to live animated bones
      if (this._showVitalOrgans && this._vitalOrgans.length) {
        const segs = 20;
        for (const org of this._vitalOrgans) {
          let bIdx = -1;
          for (let i = 0; i < n; i++) { if (joints[i].name === org.bone) { bIdx = i; break; } }
          if (bIdx < 0) continue;
          const wo = bIdx * 16;
          const ox = org.offsetRage[0], oy = org.offsetRage[1], oz = org.offsetRage[2];
          // Transform local RAGE offset by bone world matrix, then RAGE Z-up -> Y-up
          const rx = worlds[wo]*ox + worlds[wo+4]*oy + worlds[wo+8]*oz + worlds[wo+12];
          const ry = worlds[wo+1]*ox + worlds[wo+5]*oy + worlds[wo+9]*oz + worlds[wo+13];
          const rz = worlds[wo+2]*ox + worlds[wo+6]*oy + worlds[wo+10]*oz + worlds[wo+14];
          const cx = rx, cy = rz, cz = -ry;
          const r = org.radius || 0.05;
          const c = org.color || [1, 0.2, 0.2];
          pts.push(cx, cy, cz, c[0], c[1], c[2], 10.0);
          for (let s = 0; s < segs; s++) {
            const a0 = (s / segs) * Math.PI * 2, a1 = ((s + 1) / segs) * Math.PI * 2;
            const c0 = Math.cos(a0) * r, s0 = Math.sin(a0) * r;
            const c1 = Math.cos(a1) * r, s1 = Math.sin(a1) * r;
            lines.push(cx + c0, cy + s0, cz, c[0], c[1], c[2], 1.0, cx + c1, cy + s1, cz, c[0], c[1], c[2], 1.0);
            lines.push(cx + c0, cy, cz + s0, c[0], c[1], c[2], 1.0, cx + c1, cy, cz + s1, c[0], c[1], c[2], 1.0);
            lines.push(cx, cy + c0, cz + s0, c[0], c[1], c[2], 1.0, cx, cy + c1, cz + s1, c[0], c[1], c[2], 1.0);
          }
        }
      }
      const gl = this.gl;
      gl.bindBuffer(gl.ARRAY_BUFFER, this._skelLineBuf);
      gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(lines), gl.DYNAMIC_DRAW);
      this._skelLineCount = lines.length / 7;
      gl.bindBuffer(gl.ARRAY_BUFFER, this._skelPtBuf);
      gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(pts), gl.DYNAMIC_DRAW);
      this._skelPtCount = pts.length / 7;
    }
    setView(preset) {
      const presets = { iso: [0.7, 0.35], front: [0, 0.06], side: [Math.PI / 2, 0.06], top: [0, 1.48] };
      const p = presets[preset];
      if (p) { this.cam.yaw = p[0]; this.cam.pitch = p[1]; }
      this.fit();
    }
    fit() {
      if (!this.bounds) return;
      const [lo, hi] = this.bounds;
      this.cam.target = [(lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, (lo[2] + hi[2]) / 2];
      this.cam.dist = Math.max(Math.hypot(hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2]), 0.05) * 1.25;
      this.draw();
    }
    eye() {
      const c = this.cam;
      return [c.target[0] + c.dist * Math.cos(c.pitch) * Math.sin(c.yaw), c.target[1] + c.dist * Math.sin(c.pitch),
        c.target[2] + c.dist * Math.cos(c.pitch) * Math.cos(c.yaw)];
    }
    draw() {
      if (this.pending) return;
      this.pending = requestAnimationFrame((now) => {
        this.pending = 0;
        this._drainUploads(6.0);
        const dt = this.lastTick ? Math.min(0.1, Math.max(0, (now - this.lastTick) / 1000)) : 0;
        if (this.opts.spin && !this.dragging && this.prims.length) {
          this.cam.yaw += dt * 0.55;
        }
        if (this.studio.autoRotate && this.prims.length && dt > 0) {
          this.studio.rotation = (this.studio.rotation + dt * 28) % 360;
          if (this._onStudioTick) this._onStudioTick(this.studio);
        }
        const act = this._activePlayback();
        let poseDirty = false;
        if (act && this._animState.playing && act.duration > 0) {
          let t = this._animState.time + dt * (this._animState.speed || 1.0);
          if (this._animState.loop) {
            t = ((t % act.duration) + act.duration) % act.duration;
          } else if (t >= act.duration) {
            t = act.duration;
            this._animState.playing = false;
          }
          this._animState.time = t;
          poseDirty = true;
          if (this._onAnimTick) this._onAnimTick(this._animState, act);
        }
        const jiggleAlive = dt > 0 ? this._stepJiggle(dt) : false;
        if (poseDirty || jiggleAlive) {
          if (this._skel) this._evaluatePose();
        }
        const pf = this.postFx;
        const fxAnimAlive = (this.charFx.wetness > 0.005 && this.charFx.droplets > 0.01) ||
                            (this.charFx.underwater > 0.005 && this.charFx.caustics > 0.01) ||
                            (pf.active && pf.animate);
        if (fxAnimAlive && dt > 0) {
          this._fxTime += dt;
          if (pf.active && pf.animate && pf.preset) {
            const ax = pf.preset.axis || "beat";
            const hz = ax === "heartbeat" ? 1.3 : (ax === "breathRatio" ? 0.25 : (ax === "taser" ? 9.5 : ((pf.bpm || 126) / 60.0)));
            pf.phase = (pf.phase + dt * hz) % 1.0;
            this._samplePostFxCurve();
            if (this._onPostFxTick) this._onPostFxTick(pf);
          }
        }
        this.lastTick = now;
        this.render();
        const actAfter = this._activePlayback();
        const animAlive = !!(actAfter && this._animState.playing && actAfter.duration > 0);
        if ((this.opts.spin && this.prims.length) || (this.studio.autoRotate && this.prims.length) || animAlive || jiggleAlive || fxAnimAlive || this._uploadQueue.length) this.draw();
      });
    }
    setStudio(patch) {
      if (!patch || typeof patch !== "object") return this.studio;
      Object.assign(this.studio, patch);
      if (patch.autoRotate) this.lastTick = performance.now();
      this.draw();
      return this.studio;
    }
    resetStudio() {
      this.studio = {
        lightPreset: "studio_neutral",
        bgPreset: "match",
        bgCustomHex: "#1c252b",
        rotation: 0,
        exposure: 1.0,
        keyIntensity: 1.0,
        ambientIntensity: 1.0,
        shadowStrength: 0.76,
        bgBrightness: 1.0,
        autoRotate: false,
      };
      this.draw();
      return this.studio;
    }
    getStudio() { return { ...this.studio }; }
    onStudioTick(fn) { this._onStudioTick = fn; }
    _evalStudioState() {
      const st = this.studio || {};
      const lp = STUDIO_LIGHT_PRESETS.find((x) => x.id === st.lightPreset) || STUDIO_LIGHT_PRESETS[0];
      const bp = STUDIO_BG_PRESETS.find((x) => x.id === st.bgPreset) || STUDIO_BG_PRESETS[0];
      const rotRad = ((Number(st.rotation) || 0) * Math.PI) / 180;
      const kScale = Math.max(0, Number(st.keyIntensity ?? 1.0));
      const aScale = Math.max(0, Number(st.ambientIntensity ?? 1.0));
      const exp = Math.max(0.2, Math.min(3.0, Number(st.exposure ?? 1.0)));
      const shStr = Math.max(0, Math.min(1, Number(st.shadowStrength ?? 0.76)));
      const bgBright = Math.max(0.15, Math.min(2.2, Number(st.bgBrightness ?? 1.0)));
      const keyDir = rotYVec3(lp.keyDir, rotRad);
      const fillDir = rotYVec3(lp.fillDir, rotRad);
      const rimDir = rotYVec3(lp.rimDir, rotRad);
      const mul3 = (c, s) => [c[0] * s, c[1] * s, c[2] * s];
      const keyCol = mul3(lp.keyCol, kScale);
      const fillCol = mul3(lp.fillCol, 0.35 + 0.65 * aScale);
      const rimCol = mul3(lp.rimCol, 0.45 + 0.55 * kScale);
      const ambBot = mul3(lp.ambBot, aScale);
      const ambTop = mul3(lp.ambTop, aScale);
      const envScale = 0.3 + 0.7 * aScale;
      const envGround = mul3(lp.envGround, envScale);
      const envHorizon = mul3(lp.envHorizon, envScale);
      const envZenith = mul3(lp.envZenith, envScale);
      const ambCol = [(ambBot[0] + ambTop[0]) * 0.5, (ambBot[1] + ambTop[1]) * 0.5, (ambBot[2] + ambTop[2]) * 0.5];
      let bgMode = bp.mode | 0;
      let bgTop = bp.top || [0.09, 0.13, 0.16];
      let bgMid = bp.mid || [0.11, 0.15, 0.18];
      let bgBot = bp.bot || [0.05, 0.08, 0.095];
      let bgVig = bp.vig ?? 0.36;
      let gridCol = bp.grid || [0.18, 0.24, 0.26];
      if (bp.id === "match" && lp.matchBg) {
        bgMode = lp.matchBg.mode | 0;
        bgTop = lp.matchBg.top;
        bgMid = lp.matchBg.mid;
        bgBot = lp.matchBg.bot;
        bgVig = lp.matchBg.vig;
        gridCol = lp.matchBg.grid;
      } else if (bp.id === "solid") {
        bgMode = 2;
        bgMid = hexToRgb01(st.bgCustomHex, [0.11, 0.145, 0.17]);
        const lum = bgMid[0] * 0.299 + bgMid[1] * 0.587 + bgMid[2] * 0.114;
        gridCol = lum > 0.5 ? [bgMid[0] * 0.72, bgMid[1] * 0.72, bgMid[2] * 0.72] : [Math.min(1, bgMid[0] + 0.14), Math.min(1, bgMid[1] + 0.16), Math.min(1, bgMid[2] + 0.18)];
      } else if (bp.id === "env_sky") {
        bgMode = 1;
        const hLum = (lp.envHorizon[0] + lp.envHorizon[1] + lp.envHorizon[2]) / 3;
        gridCol = hLum > 0.55 ? [0.42, 0.46, 0.50] : [0.22, 0.28, 0.32];
      }
      return {
        keyDir, keyCol, fillDir, fillCol, rimDir, rimCol,
        ambBot, ambTop, ambCol,
        envGround, envHorizon, envZenith,
        envStrip: lp.envStrip ?? 0.8,
        exposure: exp,
        shadowMin: 1.0 - shStr,
        bgMode, bgTop, bgMid, bgBot, bgVig, bgBright, gridCol,
      };
    }
    setProcedural(patch) {
      Object.assign(this.procedural, patch || {});
      this.draw();
    }
    setSharedLutArray(hash, tarrBuffer) {
      if (!hash || !tarrBuffer || tarrBuffer.byteLength < 16) return null;
      const dv = new DataView(tarrBuffer);
      if (dv.getUint8(0) !== 84 || dv.getUint8(1) !== 65 || dv.getUint8(2) !== 82 || dv.getUint8(3) !== 82) return null;
      const width = dv.getUint32(4, true), height = dv.getUint32(8, true), slices = dv.getUint32(12, true);
      const need = width * height * slices * 4;
      if (tarrBuffer.byteLength < 16 + need) return null;
      const pixels = new Uint8Array(tarrBuffer, 16, need);
      const gl = this.gl;
      const prev = this._lutArrays.get(hash);
      if (prev && prev.tex) gl.deleteTexture(prev.tex);
      const tex = gl.createTexture();
      gl.bindTexture(gl.TEXTURE_2D_ARRAY, tex);
      gl.texImage3D(gl.TEXTURE_2D_ARRAY, 0, gl.RGBA, width, height, slices, 0, gl.RGBA, gl.UNSIGNED_BYTE, pixels);
      gl.texParameteri(gl.TEXTURE_2D_ARRAY, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D_ARRAY, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D_ARRAY, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
      gl.texParameteri(gl.TEXTURE_2D_ARRAY, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
      const entry = { tex, width, height, slices, pixels };
      this._lutArrays.set(hash, entry);
      this.draw();
      return entry;
    }
    setSharedLutImage(hash, image) {
      if (!hash || !image) return null;
      const gl = this.gl;
      const prev = this._lutArrays.get(hash);
      if (prev && prev.tex) gl.deleteTexture(prev.tex);
      const tex = gl.createTexture();
      gl.bindTexture(gl.TEXTURE_2D_ARRAY, tex);
      gl.texImage3D(gl.TEXTURE_2D_ARRAY, 0, gl.RGBA, image.width, image.height, 1, 0, gl.RGBA, gl.UNSIGNED_BYTE, image);
      gl.texParameteri(gl.TEXTURE_2D_ARRAY, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D_ARRAY, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D_ARRAY, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
      gl.texParameteri(gl.TEXTURE_2D_ARRAY, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
      const entry = { tex, width: image.width, height: image.height, slices: 1, pixels: null };
      this._lutArrays.set(hash, entry);
      this.draw();
      return entry;
    }
    setSharedLivery(image) {
      if (this._sharedLiveryTex) { this.gl.deleteTexture(this._sharedLiveryTex); this._sharedLiveryTex = null; }
      if (image) this._sharedLiveryTex = this._uploadTexture(image, false);
      this.draw();
    }
    render() {
      const gl = this.gl, c = this.canvas, dpr = Math.min(devicePixelRatio || 1, 2);
      const w = Math.max(1, Math.round(c.clientWidth * dpr)), h = Math.max(1, Math.round(c.clientHeight * dpr));
      if (c.width !== w || c.height !== h) { c.width = w; c.height = h; }
      const st = this._evalStudioState();
      const eye = this.eye(), d = this.cam.dist;
      const aspect = w / Math.max(1, h);
      const fov = 0.75;
      gl.bindFramebuffer(gl.FRAMEBUFFER, null);
      const usePost = !!(this.postProg && this.postFx.active && (this.postFx.intensity || 0) > 0.005);
      if (usePost && this._ensurePostFbo(w, h)) {
        gl.bindFramebuffer(gl.FRAMEBUFFER, this._postFbo);
      }
      gl.viewport(0, 0, w, h);
      gl.clearColor(st.bgMid[0], st.bgMid[1], st.bgMid[2], 1);
      gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
      // Draw procedural studio backdrop / 3D environment dome behind scene
      if (this.bgProg) {
        const camFwd = norm(sub(this.cam.target, eye));
        const refUp = Math.abs(camFwd[1]) > 0.995 ? [0, 0, -1] : [0, 1, 0];
        const camRight = norm(cross(camFwd, refUp));
        const camUp = cross(camRight, camFwd);
        gl.disable(gl.DEPTH_TEST);
        gl.depthMask(false);
        gl.disable(gl.BLEND);
        gl.useProgram(this.bgProg);
        gl.uniform1i(this.bgLoc.bgMode, st.bgMode);
        gl.uniform3fv(this.bgLoc.bgTop, st.bgTop);
        gl.uniform3fv(this.bgLoc.bgMid, st.bgMid);
        gl.uniform3fv(this.bgLoc.bgBot, st.bgBot);
        gl.uniform1f(this.bgLoc.bgVignette, st.bgVig);
        gl.uniform1f(this.bgLoc.bgBright, st.bgBright);
        gl.uniform3fv(this.bgLoc.camRight, camRight);
        gl.uniform3fv(this.bgLoc.camUp, camUp);
        gl.uniform3fv(this.bgLoc.camFwd, camFwd);
        gl.uniform1f(this.bgLoc.aspect, aspect);
        gl.uniform1f(this.bgLoc.tanHalfFov, Math.tan(fov * 0.5));
        gl.uniform3fv(this.bgLoc.uEnvGround, st.envGround);
        gl.uniform3fv(this.bgLoc.uEnvHorizon, st.envHorizon);
        gl.uniform3fv(this.bgLoc.uEnvZenith, st.envZenith);
        gl.uniform3fv(this.bgLoc.uKeyDir, st.keyDir);
        gl.uniform3fv(this.bgLoc.uKeyCol, st.keyCol);
        gl.uniform3fv(this.bgLoc.uFillDir, st.fillDir);
        gl.uniform3fv(this.bgLoc.uFillCol, st.fillCol);
        gl.uniform3fv(this.bgLoc.uRimDir, st.rimDir);
        gl.uniform3fv(this.bgLoc.uRimCol, st.rimCol);
        gl.uniform1f(this.bgLoc.uEnvStrip, st.envStrip);
        gl.uniform1f(this.bgLoc.uExposure, st.exposure);
        gl.bindVertexArray(this._postVao);
        gl.drawArrays(gl.TRIANGLES, 0, 3);
        gl.depthMask(true);
      }
      if (!this.prims.length && !(this.opts.skeleton && this._skel)) {
        if (usePost) gl.bindFramebuffer(gl.FRAMEBUFFER, null);
        return;
      }
      gl.enable(gl.DEPTH_TEST);
      const mode = this.opts.mode;
      const ident = new Float32Array([1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]);
      gl.activeTexture(gl.TEXTURE14);
      gl.bindTexture(gl.TEXTURE_2D, this._boneTex);
      let lightVP = ident, useShadow = false, shadowBias = 0.002;
      if (this.shadowReady && this.bounds && this.prims.length && mode !== "wire" && st.shadowMin < 0.995) {
        const [lo, hi] = this.bounds;
        const cx = (lo[0] + hi[0]) * 0.5, cy = (lo[1] + hi[1]) * 0.5, cz = (lo[2] + hi[2]) * 0.5;
        const rad = Math.max(0.08, Math.hypot(hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2]) * 0.56);
        const l1 = st.keyDir;
        const le = [cx + l1[0] * rad * 2.6, cy + l1[1] * rad * 2.6, cz + l1[2] * rad * 2.6];
        const upVec = Math.abs(l1[1]) > 0.96 ? [0, 0, 1] : [0, 1, 0];
        lightVP = mat.mul(mat.ortho(-rad, rad, -rad, rad, rad * 0.1, rad * 5.2), mat.look(le, [cx, cy, cz], upVec));
        shadowBias = Math.max(0.0005, rad * 0.0025);
        gl.bindFramebuffer(gl.FRAMEBUFFER, this.shadowFbo);
        gl.viewport(0, 0, this.shadowSize, this.shadowSize);
        gl.clear(gl.DEPTH_BUFFER_BIT);
        gl.colorMask(false, false, false, false);
        gl.disable(gl.CULL_FACE);
        gl.enable(gl.POLYGON_OFFSET_FILL);
        gl.polygonOffset(2.0, 4.0);
        gl.useProgram(this.shadowProg);
        gl.uniform1i(this.shadowLocBoneTex, 14);
        for (const p of this.prims) {
          const m = this.materials.get(p.layer + ":" + p.shader);
          if (m && (!m.visible || m.matMode === 2 || (m.passType || 0) >= 2)) continue;
          const skOn = !!(this._skel && p.skinned);
          gl.uniform1i(this.shadowLocUseSkin, skOn ? 1 : 0);
          gl.uniform1i(this.shadowLocHasSkin8, (skOn && p.hasSkin8) ? 1 : 0);
          gl.uniformMatrix4fv(this.shadowLocMvp, false, mat.mul(lightVP, p.model));
          gl.bindVertexArray(p.vao);
          gl.drawElements(gl.TRIANGLES, p.count, p.itype, 0);
        }
        gl.disable(gl.POLYGON_OFFSET_FILL);
        gl.colorMask(true, true, true, true);
        gl.bindFramebuffer(gl.FRAMEBUFFER, usePost ? this._postFbo : null);
        gl.viewport(0, 0, w, h);
        useShadow = true;
      }
      gl.useProgram(this.prog);
      const vp = mat.mul(mat.persp(fov, aspect, d / 500, d * 50), mat.look(eye, this.cam.target, [0, 1, 0]));
      gl.uniform3fv(this.loc.eye, eye);
      gl.uniform1i(this.loc.flipV, this.opts.flipv);
      gl.uniform1i(this.loc.alphaCut, this.opts.alpha);
      gl.uniform1i(this.loc.useSkin, 0);
      gl.uniform1i(this.loc.hasSkin8, 0);
      gl.uniform1i(this.loc.tex, 0);
      gl.uniform1i(this.loc.normTex, 1);
      gl.uniform1i(this.loc.layer1Tex, 2);
      gl.uniform1i(this.loc.layer2Tex, 3);
      gl.uniform1i(this.loc.layer3Tex, 4);
      gl.uniform1i(this.loc.maskTex, 5);
      gl.uniform1i(this.loc.tintTex, 6);
      gl.uniform1i(this.loc.tintPalTex, 7);
      gl.uniform1i(this.loc.specTex, 8);
      gl.uniform1i(this.loc.spec2Tex, 9);
      gl.uniform1i(this.loc.dustTex, 10);
      gl.uniform1i(this.loc.liveryTex, 11);
      gl.uniform1i(this.loc.lutArrayTex, 12);
      gl.uniform1i(this.loc.shadowTex, 13);
      gl.uniform1i(this.loc.boneTex, 14);
      gl.uniformMatrix4fv(this.loc.lightVP, false, lightVP);
      gl.uniform1i(this.loc.useShadow, useShadow ? 1 : 0);
      gl.uniform1f(this.loc.shadowBias, shadowBias);
      gl.uniform3fv(this.loc.uKeyDir, st.keyDir);
      gl.uniform3fv(this.loc.uKeyCol, st.keyCol);
      gl.uniform3fv(this.loc.uFillDir, st.fillDir);
      gl.uniform3fv(this.loc.uFillCol, st.fillCol);
      gl.uniform3fv(this.loc.uRimDir, st.rimDir);
      gl.uniform3fv(this.loc.uRimCol, st.rimCol);
      gl.uniform3fv(this.loc.uEnvGround, st.envGround);
      gl.uniform3fv(this.loc.uEnvHorizon, st.envHorizon);
      gl.uniform3fv(this.loc.uEnvZenith, st.envZenith);
      gl.uniform3fv(this.loc.uAmbBot, st.ambBot);
      gl.uniform3fv(this.loc.uAmbTop, st.ambTop);
      gl.uniform1f(this.loc.uExposure, st.exposure);
      gl.uniform1f(this.loc.uEnvStrip, st.envStrip);
      gl.uniform1f(this.loc.uShadowMin, st.shadowMin);
      gl.activeTexture(gl.TEXTURE12);
      gl.bindTexture(gl.TEXTURE_2D_ARRAY, this._dummyArrayTex);
      gl.activeTexture(gl.TEXTURE13);
      gl.bindTexture(gl.TEXTURE_2D, useShadow ? this.shadowTex : this._dummyShadowTex);
      const pr = this.procedural;
      gl.uniform3fv(this.loc.vehPrimary, pr.vehPrimary);
      gl.uniform3fv(this.loc.vehSecondary, pr.vehSecondary);
      gl.uniform3fv(this.loc.vehPearlCol, pr.vehPearlCol);
      gl.uniform1f(this.loc.vehPaintRow1, pr.vehPaintRow1);
      gl.uniform1f(this.loc.vehPaintRow2, pr.vehPaintRow2);
      gl.uniform1f(this.loc.vehPaintSlice, pr.vehPaintSlice);
      gl.uniform1f(this.loc.vehPearl, pr.vehPearl);
      gl.uniform1f(this.loc.vehClearcoat, pr.vehClearcoat);
      gl.uniform1f(this.loc.vehDirt, pr.vehDirt);
      gl.uniform1f(this.loc.vehWear, pr.vehWear);
      gl.uniform1i(this.loc.vehTwoTone, pr.vehTwoTone | 0);
      gl.uniform1i(this.loc.vehInterior, pr.vehInterior | 0);
      gl.uniform1i(this.loc.vehLutExact, pr.vehLutExact ? 1 : 0);
      gl.uniform1i(this.loc.wpnFinish, pr.wpnFinish | 0);
      gl.uniform1f(this.loc.wpnWear, pr.wpnWear);
      gl.uniform1f(this.loc.liveryScale, pr.liveryScale || 1.0);
      // Character Weathering, SunTan, Blood, Wounds & Drug Lighting uniforms
      const cf = this.charFx;
      const cfOn = this._hasActiveCharFx();
      gl.uniform1i(this.loc.charFxOn, cfOn ? 1 : 0);
      gl.uniform1i(this.loc.bloodDecalTex, 15);
      gl.activeTexture(gl.TEXTURE15);
      gl.bindTexture(gl.TEXTURE_2D, this._bloodDecalTex || this._dummy2DTex);
      gl.uniform1i(this.loc.useBloodDecal, this._bloodDecalTex ? 1 : 0);
      gl.uniform1i(this.loc.bloodDecalTintable, this._bloodDecalTintable ? 1 : 0);
      if (cfOn) {
        gl.uniform1f(this.loc.charTime, this._fxTime);
        gl.uniform1f(this.loc.charWetness, cf.wetness || 0);
        gl.uniform1f(this.loc.charSoak, cf.soak || 0);
        gl.uniform1f(this.loc.charWaterHeight, cf.waterHeight ?? 2.2);
        gl.uniform1f(this.loc.charDroplets, cf.droplets ?? 0.55);
        gl.uniform1f(this.loc.charEyeWetness, cf.eyeWetness ?? 0.2);
        gl.uniform1f(this.loc.charDust, cf.dust || 0);
        gl.uniform1f(this.loc.charDirtAmt, cf.dirtAmt || 0);
        gl.uniform3fv(this.loc.charDirtCol, cf.dirtCol || [0.3, 0.23, 0.17]);
        gl.uniform2fv(this.loc.charDirtRough, cf.dirtRough || [0.2, 0.6]);
        gl.uniform1f(this.loc.charDirtSkinStr, cf.dirtSkinStr ?? 0.5);
        gl.uniform1f(this.loc.charDirtClothStr, cf.dirtClothStr ?? 0.7);
        gl.uniform1f(this.loc.charUnderwater, cf.underwater || 0);
        gl.uniform1f(this.loc.charWaterLine, cf.waterLine ?? 1.85);
        gl.uniform1f(this.loc.charCaustics, cf.caustics ?? 0.75);
        gl.uniform1f(this.loc.charSunTan, cf.sunTan || 0);
        gl.uniform1f(this.loc.charSunBurn, cf.sunBurn || 0);
        gl.uniform1i(this.loc.charTanLines, cf.tanLines | 0);
        gl.uniform1f(this.loc.charFlush, cf.flush || 0);
        gl.uniform1f(this.loc.charGlints, cf.glints || 0);
        gl.uniform1f(this.loc.charPedLightMult, cf.pedLightMult ?? 1.0);
        gl.uniform1f(this.loc.charPedFresnel, cf.pedFresnel || 0);
        gl.uniform1f(this.loc.charUvParty, cf.uvParty || 0);
        gl.uniform1f(this.loc.charBloodSoak, cf.bloodSoak || 0);
        const ws = cf.wounds || [];
        gl.uniform1i(this.loc.charWoundCount, Math.min(8, ws.length));
        if (ws.length) {
          const wpArr = new Float32Array(32), wprArr = new Float32Array(32);
          for (let i = 0; i < Math.min(8, ws.length); i++) {
            const w0 = ws[i];
            wpArr[i*4] = w0.pos[0]; wpArr[i*4+1] = w0.pos[1]; wpArr[i*4+2] = w0.pos[2]; wpArr[i*4+3] = w0.radius || 0.065;
            wprArr[i*4] = w0.kind || 0; wprArr[i*4+1] = w0.intensity ?? 1.0; wprArr[i*4+2] = w0.drip ?? 0.14; wprArr[i*4+3] = w0.seed ?? (i + 1.3);
          }
          gl.uniform4fv(this.loc.charWoundPos, wpArr);
          gl.uniform4fv(this.loc.charWoundParam, wprArr);
        }
      }
      if (this.opts.grid && this.grid) {
        gl.uniformMatrix4fv(this.loc.mvp, false, vp); gl.uniformMatrix4fv(this.loc.model, false, ident);
        gl.uniform1i(this.loc.mode, 9); gl.uniform3fv(this.loc.color, st.gridCol);
        gl.bindVertexArray(this.grid.vao); gl.drawArrays(gl.LINES, 0, this.grid.count);
      }
      if (this.opts.box && this.boxMesh) {
        gl.uniformMatrix4fv(this.loc.mvp, false, vp); gl.uniformMatrix4fv(this.loc.model, false, ident);
        gl.uniform1i(this.loc.mode, 9); gl.uniform3f(this.loc.color, 0.72, 0.62, 0.34);
        gl.bindVertexArray(this.boxMesh.vao); gl.drawArrays(gl.LINES, 0, this.boxMesh.count);
      }
      if (this.opts.backfaces) gl.disable(gl.CULL_FACE); else { gl.enable(gl.CULL_FACE); gl.cullFace(gl.BACK); gl.frontFace(gl.CW); }
      const surfaces = mode !== "wire";
      const decalPrims = [], blendPrims = [];
      const drawPrim = (p, m, key, blendPass, isDecal) => {
        const skOn = !!(this._skel && p.skinned);
        gl.uniform1i(this.loc.useSkin, skOn ? 1 : 0);
        gl.uniform1i(this.loc.hasSkin8, (skOn && p.hasSkin8) ? 1 : 0);
        gl.uniformMatrix4fv(this.loc.mvp, false, mat.mul(vp, p.model));
        gl.uniformMatrix4fv(this.loc.model, false, p.model);
        if (surfaces) {
          gl.enable(gl.POLYGON_OFFSET_FILL);
          gl.polygonOffset(isDecal ? -1 : 1, isDecal ? -2 : 1);
          gl.uniform1i(this.loc.mode, 0);
          gl.uniform1i(this.loc.hasNormals, p.hasNormals);
          gl.uniform1i(this.loc.highlight, this.highlightKey === key);
          const texMode = mode === "textured" ? (m.texMode || 0) : 0;
          const canUV = p.hasUV || (texMode === 5 && p.hasUV1);
          const textured = mode === "textured" && m.texture && canUV;
          const vertexColored = mode === "textured" && !textured && texMode !== 6 && texMode !== 7 && p.hasColor;
          const normalMapped = (mode === "textured" || mode === "solid") && this.opts.normals && m.normalTexture && (p.hasUV || p.hasUV1);
          const matMode = mode === "textured" ? (m.matMode || 0) : 0;
          const passType = mode === "textured" ? (m.passType || 0) : 0;
          const useTintPal = mode === "textured" && !!m.tintPalTex && (m.tintPalWidth || 0) > 0 && p.hasTintIndex && (texMode === 0 || texMode === 5 || !textured);
          const useSpec = mode === "textured" && !!m.specTex && (p.hasUV || p.hasUV1);
          const useSpec2 = mode === "textured" && !!m.spec2Tex && p.hasUV;
          const useDust = mode === "textured" && !!m.dustTex && p.hasUV;
          const useLivery = mode === "textured" && texMode === 7 && !!this._sharedLiveryTex && p.hasUV;
          const lutEntry = (mode === "textured" && texMode === 6 && m.lutHash) ? this._lutArrays.get(m.lutHash) : null;
          gl.uniform1i(this.loc.useTex, !!textured);
          gl.uniform1i(this.loc.texMode, texMode);
          gl.uniform1i(this.loc.matMode, matMode);
          gl.uniform1i(this.loc.passType, passType);
          gl.uniform1i(this.loc.useColor, !!vertexColored);
          gl.uniform1i(this.loc.useNorm, !!normalMapped);
          gl.uniform1i(this.loc.useTintPal, !!useTintPal);
          gl.uniform1i(this.loc.useSpec, !!useSpec);
          gl.uniform1i(this.loc.useSpec2, !!useSpec2);
          gl.uniform1i(this.loc.useDust, !!useDust);
          gl.uniform1i(this.loc.useLivery, !!useLivery);
          gl.uniform1i(this.loc.useLutArray, !!lutEntry);
          gl.uniform1i(this.loc.lutSlices, lutEntry ? lutEntry.slices : 0);
          gl.uniform1i(this.loc.tintRow, m.tintRow || 0);
          gl.uniform1i(this.loc.tintRowLen, m.tintRowLen || m.tintPalWidth || 1);
          gl.uniform1i(this.loc.tintPalWidth, m.tintPalWidth || 0);
          gl.uniform1i(this.loc.hasUV1, !!p.hasUV1);
          gl.uniform1i(this.loc.hasBlendW, !!p.hasBlendW);
          gl.uniform1i(this.loc.hasVertAlpha, !!p.hasVertAlpha);
          gl.uniform1i(this.loc.lumaAlpha, !!m.lumaAlpha);
          let surfColor = m.color;
          if (mode === "solid") surfColor = [0.72, 0.73, 0.7];
          else if (mode === "textured") {
            if (texMode === 6) surfColor = m.tintColor || [0.46, 0.48, 0.52];
            else if (textured && texMode === 1) surfColor = m.tintColor || [0.68, 0.65, 0.6];
            else if (textured && texMode === 3) surfColor = m.tintColor || [0.76, 0.57, 0.47];
            else if (textured) surfColor = m.tintOverride && m.tintColor ? m.tintColor : [1, 1, 1];
            else surfColor = m.tintColor || [0.72, 0.73, 0.7];
          }
          gl.uniform3fv(this.loc.color, surfColor);
          gl.uniform3fv(this.loc.skinGain, (mode === "textured" && m.skinGain && m.skinGain.length === 3) ? m.skinGain : [1, 1, 1]);
          if (textured) {
            if (texMode === 2) {
              const pal = m.palette || [[0.46, 0.34, 0.23], [0.62, 0.42, 0.25], [0.90, 0.88, 0.84]];
              gl.uniform3fv(this.loc.pal0, pal[0]);
              gl.uniform3fv(this.loc.pal1, pal[1]);
              gl.uniform3fv(this.loc.pal2, pal[2]);
            }
            gl.activeTexture(gl.TEXTURE0);
            gl.bindTexture(gl.TEXTURE_2D, m.texture);
            if (texMode === 5) {
              gl.uniform1i(this.loc.hasL1, !!m.layer1Tex);
              gl.uniform1i(this.loc.hasL2, !!m.layer2Tex);
              gl.uniform1i(this.loc.hasL3, !!m.layer3Tex);
              gl.uniform1i(this.loc.useMask, !!(m.maskTex && p.hasUV));
              gl.uniform1i(this.loc.useBilinearMask, !!m.useBilinearMask);
              gl.uniform1i(this.loc.useTintTex, !!(m.tintTex && p.hasUV));
              if (m.layer1Tex) { gl.activeTexture(gl.TEXTURE2); gl.bindTexture(gl.TEXTURE_2D, m.layer1Tex); }
              if (m.layer2Tex) { gl.activeTexture(gl.TEXTURE3); gl.bindTexture(gl.TEXTURE_2D, m.layer2Tex); }
              if (m.layer3Tex) { gl.activeTexture(gl.TEXTURE4); gl.bindTexture(gl.TEXTURE_2D, m.layer3Tex); }
              if (m.maskTex) { gl.activeTexture(gl.TEXTURE5); gl.bindTexture(gl.TEXTURE_2D, m.maskTex); }
              if (m.tintTex) { gl.activeTexture(gl.TEXTURE6); gl.bindTexture(gl.TEXTURE_2D, m.tintTex); }
            } else {
              gl.uniform1i(this.loc.hasL1, false);
              gl.uniform1i(this.loc.hasL2, false);
              gl.uniform1i(this.loc.hasL3, false);
              gl.uniform1i(this.loc.useMask, false);
              gl.uniform1i(this.loc.useBilinearMask, false);
              gl.uniform1i(this.loc.useTintTex, false);
            }
          }
          if (normalMapped) { gl.activeTexture(gl.TEXTURE1); gl.bindTexture(gl.TEXTURE_2D, m.normalTexture); }
          if (useTintPal) { gl.activeTexture(gl.TEXTURE7); gl.bindTexture(gl.TEXTURE_2D, m.tintPalTex); }
          if (useSpec) { gl.activeTexture(gl.TEXTURE8); gl.bindTexture(gl.TEXTURE_2D, m.specTex); }
          if (useSpec2) { gl.activeTexture(gl.TEXTURE9); gl.bindTexture(gl.TEXTURE_2D, m.spec2Tex); }
          if (useDust) { gl.activeTexture(gl.TEXTURE10); gl.bindTexture(gl.TEXTURE_2D, m.dustTex); }
          if (useLivery) { gl.activeTexture(gl.TEXTURE11); gl.bindTexture(gl.TEXTURE_2D, this._sharedLiveryTex); }
          gl.activeTexture(gl.TEXTURE12);
          gl.bindTexture(gl.TEXTURE_2D_ARRAY, lutEntry ? lutEntry.tex : this._dummyArrayTex);
          gl.bindVertexArray(p.vao);
          gl.drawElements(gl.TRIANGLES, p.count, p.itype, 0);
          gl.disable(gl.POLYGON_OFFSET_FILL);
        }
        if (!blendPass && (mode === "wire" || this.opts.overlay)) {
          gl.uniform1i(this.loc.mode, 9);
          gl.uniform3fv(this.loc.color, mode === "wire" ? m.color.map((x) => Math.min(1, x * 1.15 + 0.1)) : [0.06, 0.08, 0.09]);
          gl.bindVertexArray(p.evao);
          gl.drawElements(gl.LINES, p.ecount, p.etype, 0);
        }
      };
      const drawWireOverlay = (p) => {
        if (!this.opts.overlay) return;
        const skOn = !!(this._skel && p.skinned);
        gl.uniform1i(this.loc.useSkin, skOn ? 1 : 0);
        gl.uniform1i(this.loc.hasSkin8, (skOn && p.hasSkin8) ? 1 : 0);
        gl.uniformMatrix4fv(this.loc.mvp, false, mat.mul(vp, p.model));
        gl.uniformMatrix4fv(this.loc.model, false, p.model);
        gl.uniform1i(this.loc.mode, 9);
        gl.uniform3fv(this.loc.color, [0.06, 0.08, 0.09]);
        gl.bindVertexArray(p.evao);
        gl.drawElements(gl.LINES, p.ecount, p.etype, 0);
      };
      for (const p of this.prims) {
        const key = p.layer + ":" + p.shader;
        const m = this.materials.get(key) || { visible: true, color: [0.7, 0.7, 0.7], texture: null, normalTexture: null };
        if (!m.visible) continue;
        if (mode === "textured") {
          if (m.passType === 2) {
            decalPrims.push([p, m, key]);
            drawWireOverlay(p);
            continue;
          }
          if (m.matMode === 2 || m.passType === 3 || m.passType === 4) {
            blendPrims.push([p, m, key]);
            drawWireOverlay(p);
            continue;
          }
        }
        drawPrim(p, m, key, false, false);
      }
      // Strand Groom rendering (hair, brows, lashes, beard, bodyhair) locked to live skeleton bones
      if (this.groomProg && this._grooms && this._grooms.size && mode !== "wire") {
        let standingOff = [0, 1.0, 0];
        for (const p of this.prims) {
          if (p.skinned && p.model) {
            standingOff = [p.model[12], p.model[13], p.model[14]];
            break;
          }
        }
        const findBoneIdx = (bname) => {
          if (!this._skel || !this._skel.joints) return -1;
          const joints = this._skel.joints;
          for (let i = 0; i < joints.length; i++) if (joints[i].name === bname) return i;
          if (bname === "SKEL_Head") {
            for (let i = 0; i < joints.length; i++) if (joints[i].name === "SKEL_Neck0") return i;
          }
          return 0;
        };
        gl.useProgram(this.groomProg);
        gl.enable(gl.BLEND);
        gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
        gl.depthMask(true);
        gl.uniformMatrix4fv(this.groomLoc.vp, false, vp);
        gl.uniform3fv(this.groomLoc.standingOff, standingOff);
        gl.uniform3fv(this.groomLoc.eye, eye);
        gl.uniform3fv(this.groomLoc.uKeyDir, st.keyDir);
        gl.uniform3fv(this.groomLoc.uKeyCol, st.keyCol);
        gl.uniform3fv(this.groomLoc.uFillDir, st.fillDir);
        gl.uniform3fv(this.groomLoc.uFillCol, st.fillCol);
        gl.uniform3fv(this.groomLoc.uRimDir, st.rimDir);
        gl.uniform3fv(this.groomLoc.uRimCol, st.rimCol);
        gl.uniform3fv(this.groomLoc.uAmbCol, st.ambCol);
        gl.uniform1f(this.groomLoc.uExposure, st.exposure);
        for (const g of this._grooms.values()) {
          if (!g.indexCount) continue;
          const bi = findBoneIdx(g.boneName || "SKEL_Head");
          const bMat = (bi >= 0 && this._skel) ? this._boneData.subarray(bi * 16, bi * 16 + 16) : ident;
          gl.uniformMatrix4fv(this.groomLoc.boneDelta, false, bMat);
          gl.uniform3fv(this.groomLoc.rootCol, g.rootCol);
          gl.uniform3fv(this.groomLoc.tipCol, g.tipCol);
          gl.uniform1f(this.groomLoc.alpha, g.alpha ?? 0.94);
          gl.bindVertexArray(g.vao);
          gl.drawElements(gl.LINES, g.indexCount, gl.UNSIGNED_INT, 0);
        }
        gl.disable(gl.BLEND);
        gl.useProgram(this.prog);
      }
      if (decalPrims.length || blendPrims.length) {
        gl.enable(gl.BLEND);
        gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
        gl.depthMask(false);
        for (const [p, m, key] of decalPrims) drawPrim(p, m, key, true, true);
        for (const [p, m, key] of blendPrims) drawPrim(p, m, key, true, false);
        gl.depthMask(true);
        gl.disable(gl.BLEND);
      }
      // 3D Skeleton & Vital Organs overlay (bones + joint markers + organ hitboxes).
      if ((this.opts.skeleton || this._showVitalOrgans) && this._skel && this.skelProg && (this._skelLineCount > 0 || this._skelPtCount > 0)) {
        let skelModel = null;
        for (const p of this.prims) { if (p.skinned) { skelModel = p.model; break; } }
        if (!skelModel) {
          const off = this._skel.defaultOffsetYup || [0, 0, 0];
          skelModel = new Float32Array([1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, off[0], off[1], off[2], 1]);
        }
        if (this.opts.skelXray || this._showVitalOrgans) gl.disable(gl.DEPTH_TEST);
        gl.enable(gl.BLEND);
        gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
        gl.useProgram(this.skelProg);
        gl.uniformMatrix4fv(this.skelLocVp, false, mat.mul(vp, skelModel));
        if (this._skelLineCount > 0) {
          gl.uniform1i(this.skelLocIsPoint, 0);
          gl.bindVertexArray(this._skelLineVao);
          gl.drawArrays(gl.LINES, 0, this._skelLineCount);
        }
        if (this._skelPtCount > 0) {
          gl.uniform1i(this.skelLocIsPoint, 1);
          gl.bindVertexArray(this._skelPtVao);
          gl.drawArrays(gl.POINTS, 0, this._skelPtCount);
        }
        gl.disable(gl.BLEND);
        if (this.opts.skelXray || this._showVitalOrgans) gl.enable(gl.DEPTH_TEST);
      }
      // Full-screen GPU Post-FX resolve (Mandy_*, Cocaine, Molly, Weed, Drunk, Trips, Combat)
      if (usePost && this._postFbo) {
        const pf = this.postFx;
        gl.bindFramebuffer(gl.FRAMEBUFFER, null);
        gl.viewport(0, 0, w, h);
        gl.disable(gl.DEPTH_TEST);
        gl.useProgram(this.postProg);
        gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, this._postTex);
        gl.activeTexture(gl.TEXTURE1); gl.bindTexture(gl.TEXTURE_2D, this._trailTex);
        gl.uniform1i(this.postLoc.sceneTex, 0);
        gl.uniform1i(this.postLoc.trailTex, 1);
        gl.uniform1f(this.postLoc.pfTime, this._fxTime);
        gl.uniform1f(this.postLoc.pfIntensity, pf.intensity || 0);
        gl.uniform1f(this.postLoc.pfBloom, pf.bloom || 0);
        gl.uniform1f(this.postLoc.pfLensK1, pf.lensK1 || 0);
        gl.uniform1f(this.postLoc.pfLensK2, pf.lensK2 || 0);
        gl.uniform1f(this.postLoc.pfChromK1, pf.chromK1 || 0);
        gl.uniform1f(this.postLoc.pfChromK2, pf.chromK2 || 0);
        gl.uniform3fv(this.postLoc.pfChromCol, pf.chromCol || [0, 0, 0]);
        gl.uniform3fv(this.postLoc.pfVigCol, pf.vigCol || [0, 0, 0]);
        gl.uniform1f(this.postLoc.pfVigInt, pf.vigInt || 0);
        gl.uniform1f(this.postLoc.pfVigIn, pf.vigIn ?? 0.5);
        gl.uniform1f(this.postLoc.pfVigOut, pf.vigOut ?? 2.5);
        gl.uniform1f(this.postLoc.pfVigBloom, pf.vigBloom || 0);
        gl.uniform1f(this.postLoc.pfDesat, pf.desat ?? 1.0);
        gl.uniform1f(this.postLoc.pfShCon, pf.shCon ?? 1.0);
        gl.uniform1f(this.postLoc.pfHlCon, pf.hlCon ?? 1.0);
        gl.uniform1f(this.postLoc.pfDtCon, pf.dtCon ?? 1.0);
        gl.uniform1f(this.postLoc.pfNoise, pf.noise || 0);
        gl.uniform1f(this.postLoc.pfExposure, pf.exposure || 0);
        gl.uniform1f(this.postLoc.pfTrail, pf.trail || 0);
        gl.uniform1f(this.postLoc.pfRedShadow, pf.redShadow || 0);
        gl.uniform1f(this.postLoc.pfUvParty, pf.uvParty || 0);
        gl.bindVertexArray(this._postVao);
        gl.drawArrays(gl.TRIANGLES, 0, 3);
        if ((pf.trail || 0) > 0.01 && this._trailFbo) {
          gl.bindFramebuffer(gl.READ_FRAMEBUFFER, null);
          gl.bindFramebuffer(gl.DRAW_FRAMEBUFFER, this._trailFbo);
          gl.blitFramebuffer(0, 0, w, h, 0, 0, w, h, gl.COLOR_BUFFER_BIT, gl.LINEAR);
          gl.bindFramebuffer(gl.FRAMEBUFFER, null);
        }
        gl.enable(gl.DEPTH_TEST);
      }
      gl.bindVertexArray(null);
    }
    bindInput() {
      const c = this.canvas;
      let drag = null;
      c.addEventListener("contextmenu", (e) => e.preventDefault());
      c.addEventListener("pointerdown", (e) => {
        if (this._pickMode && e.button === 0 && !e.shiftKey) {
          const hit = this.pickSurface(e.clientX, e.clientY);
          if (hit && this._onSurfacePick) {
            this._onSurfacePick(hit);
            return;
          }
        }
        this.dragging = true;
        drag = { x: e.clientX, y: e.clientY, pan: e.button === 2 || e.shiftKey, yaw: this.cam.yaw, pitch: this.cam.pitch,
          target: [...this.cam.target] };
        c.setPointerCapture(e.pointerId);
        c.focus();
      });
      c.addEventListener("pointermove", (e) => {
        if (!drag) return;
        const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
        if (drag.pan) {
          const eye = this.eye(), f = norm(sub(this.cam.target, eye)), r = norm(cross(f, [0, 1, 0])), u = cross(r, f);
          const s = this.cam.dist / c.clientHeight;
          this.cam.target = drag.target.map((t, k) => t - r[k] * dx * s + u[k] * dy * s);
        } else {
          this.cam.yaw = drag.yaw - dx * 0.008;
          this.cam.pitch = Math.max(-1.5, Math.min(1.5, drag.pitch + dy * 0.008));
        }
        this.draw();
      });
      const end = () => { drag = null; this.dragging = false; };
      c.addEventListener("pointerup", end);
      c.addEventListener("pointercancel", end);
      c.addEventListener("wheel", (e) => {
        e.preventDefault();
        this.cam.dist *= Math.exp(e.deltaY * 0.0012);
        this.draw();
      }, { passive: false });
      c.addEventListener("keydown", (e) => { if (e.key === "f" || e.key === "F") this.fit(); });
    }
  }
  window.ModelView = ModelView;
})();
