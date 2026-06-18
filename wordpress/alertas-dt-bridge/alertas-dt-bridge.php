<?php
/**
 * Plugin Name:       Alertas DT Bridge
 * Plugin URI:        https://github.com/wladimick/Alertas-DT
 * Description:       Formulario de suscripción Alertas DT y API REST para sincronización con app local.
 * Version:           0.1.0
 * Requires at least: 6.0
 * Requires PHP:      8.0
 * Author:            External Group
 * License:           GPL-2.0-or-later
 * Text Domain:       alertas-dt-bridge
 */

defined( 'ABSPATH' ) || exit;

define( 'ADT_VERSION',     '0.1.0' );
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
    ADT_Shortcode::register();
    ADT_REST::register();
    if ( is_admin() ) {
        ADT_Admin::register();
    }
} );
