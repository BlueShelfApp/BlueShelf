// BlueShelf VR viewer: reprojects one eye of a VR video to a flat camera view.
// vr_layout: 0 mono, 1 side-by-side, 2 top-bottom
// vr_proj:   0 equirect 180, 1 equirect 360, 2 fisheye (equidistant, vr_lens_fov)

//!PARAM vr_yaw
//!TYPE DYNAMIC float
0.0

//!PARAM vr_pitch
//!TYPE DYNAMIC float
0.0

//!PARAM vr_fov
//!TYPE DYNAMIC float
100.0

//!PARAM vr_eye
//!TYPE DYNAMIC int
0

//!PARAM vr_layout
//!TYPE DYNAMIC int
1

//!PARAM vr_proj
//!TYPE DYNAMIC int
0

//!PARAM vr_lens_fov
//!TYPE DYNAMIC float
190.0

//!HOOK MAINPRESUB
//!BIND HOOKED
//!WIDTH OUTPUT.w
//!HEIGHT OUTPUT.h
//!DESC BlueShelf VR flat view

const float PI = 3.14159265358979;

vec4 hook() {
    vec2 p = HOOKED_pos * 2.0 - 1.0;
    float aspect = target_size.x / target_size.y;
    float t = tan(radians(vr_fov) * 0.5);
    vec3 d = normalize(vec3(p.x * t, -p.y * t / aspect, 1.0));

    float cp = cos(vr_pitch), sp = sin(vr_pitch);
    d = vec3(d.x, d.y * cp + d.z * sp, -d.y * sp + d.z * cp);
    float cy = cos(vr_yaw), sy = sin(vr_yaw);
    d = vec3(d.x * cy + d.z * sy, d.y, -d.x * sy + d.z * cy);

    vec2 uv;
    if (vr_proj == 2) {
        float theta = acos(clamp(d.z, -1.0, 1.0));
        float r = theta / radians(vr_lens_fov * 0.5);
        if (r > 1.0)
            return vec4(0.0, 0.0, 0.0, 1.0);
        float phi = atan(d.y, d.x);
        uv = vec2(0.5 + 0.5 * r * cos(phi), 0.5 - 0.5 * r * sin(phi));
    } else {
        float span = vr_proj == 1 ? 2.0 * PI : PI;
        float lon = atan(d.x, d.z);
        if (abs(lon) > span * 0.5)
            return vec4(0.0, 0.0, 0.0, 1.0);
        float lat = asin(clamp(d.y, -1.0, 1.0));
        uv = vec2(lon / span + 0.5, 0.5 - lat / PI);
    }

    if (vr_layout == 1)
        uv.x = (uv.x + float(vr_eye)) * 0.5;
    else if (vr_layout == 2)
        uv.y = (uv.y + float(vr_eye)) * 0.5;

    return HOOKED_tex(uv);
}
