<?php
/**
 * Plugin Name:       Alertas DT + SII Bridge
 * Plugin URI:        https://github.com/wladimick/Alertas-DT
 * Description:       Formulario de suscripción Alertas DT + SII y API REST para sincronización con app local.
 * Version:           0.2.0
 * Requires at least: 6.0
 * Requires PHP:      8.0
 * Author:            External Group
 * License:           GPL-2.0-or-later
 * Text Domain:       alertas-dt-bridge
 */

defined( 'ABSPATH' ) || exit;

define( 'ADT_VERSION',     '0.2.0' );
define( 'ADT_PLUGIN_FILE', __FILE__ );
define( 'ADT_PLUGIN_DIR',  plugin_dir_path( __FILE__ ) );
define( 'ADT_PLUGIN_URL',  plugin_dir_url( __FILE__ ) );
define( 'ADT_TABLE',       'alertas_dt_subscribers' );

require_once ADT_PLUGIN_DIR . 'includes/class-adt-database.php';
require_once ADT_PLUGIN_DIR . 'includes/class-adt-activator.php';
require_once ADT_PLUGIN_DIR . 'includes/class-adt-settings.php';
require_once ADT_PLUGIN_DIR . 'includes/class-adt-shortcode.php';
require_once ADT_PLUGIN_DIR . 'includes/class-adt-rest.php';
require_once ADT_PLUGIN_DIR . 'includes/class-adt-admin.php';

register_activation_hook( __FILE__,   [ 'ADT_Activator', 'activate' ] );
register_deactivation_hook( __FILE__, [ 'ADT_Activator', 'deactivate' ] );

add_action( 'plugins_loaded', function () {
    // register_activation_hook solo se dispara en la primera activación del plugin,
    // NUNCA cuando se sube una versión nueva de archivos sobre un plugin ya activo.
    // Por eso el esquema de tabla se revisa también aquí, en cada carga.
    ADT_Activator::maybe_upgrade();

    ADT_Shortcode::register();
    ADT_REST::register();
    if ( is_admin() ) {
        ADT_Admin::register();
    }
} );
