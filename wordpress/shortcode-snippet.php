<?php
/**
 * Shortcode WordPress para insertar el formulario SaaS de Alertas DT + SII.
 *
 * Uso:
 * [dt_alertas_form base_url="https://alertas.tudominio.cl"]
 */
function dt_alertas_form_shortcode($atts) {
    $atts = shortcode_atts(
        array(
            'base_url' => 'https://alertas.tudominio.cl',
            'height' => '560',
        ),
        $atts,
        'dt_alertas_form'
    );

    $base_url = rtrim(esc_url_raw($atts['base_url']), '/');
    $source_page = home_url(add_query_arg(array(), $_SERVER['REQUEST_URI']));
    $src = esc_url($base_url . '/embed?source_page=' . rawurlencode($source_page));
    $height = intval($atts['height']);

    return sprintf(
        '<iframe src="%s" style="width:100%%;min-height:%dpx;border:0;display:block;" loading="lazy" title="Formulario Alertas DT + SII"></iframe>',
        $src,
        $height
    );
}
add_shortcode('dt_alertas_form', 'dt_alertas_form_shortcode');
